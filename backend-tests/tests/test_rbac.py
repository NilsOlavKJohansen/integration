# Copyright 2021 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
import json
import logging
import pytest
import uuid

import testutils.api.deployments as deployments
import testutils.api.deviceauth as deviceauth
import testutils.api.inventory as inventory
import testutils.api.useradm as useradm
import testutils.api.deviceconfig as deviceconfig

from testutils.common import (
    create_org,
    create_user,
    make_accepted_device,
    mongo,
    clean_mongo,
    update_tenant,
)
from testutils.util.artifact import Artifact
from testutils.api.client import ApiClient


class UserPermission:
    def __init__(self, action, permission_type, permission_target):
        if not isinstance(action, str) and isinstance(permission_type, str):
            raise AttributeError("action and permission_type must be string type")
        self.action = action
        self.object = {"type": permission_type, "value": permission_target}


class UserRole:
    def __init__(self, name, permissions):
        self.name = name
        self.permissions = []

        if isinstance(permissions, list):
            for permission in permissions:
                if not isinstance(permission, UserPermission):
                    raise AttributeError("permissions must be a list of permissions")
                self.add_permission(permission)
        elif isinstance(permissions, UserPermission):
            self.add_permission(permissions)
        else:
            raise AttributeError("permissions must be (a list) of type UserPermission")

    def json(self):
        return json.dumps(self.__dict__)

    @property
    def dict(self):
        return self.__dict__

    def add_permission(self, permission):
        self.permissions.append(permission.__dict__)


def login_tenant_users(tenant):
    useradm_MGMT = ApiClient(useradm.URL_MGMT)
    for user in tenant.users:
        rsp = useradm_MGMT.call("POST", useradm.URL_LOGIN, auth=(user.name, user.pwd))
        assert rsp.status_code == 200, "Failed to setup test environment"
        user.token = rsp.text


def setup_tenant_devices(tenant, device_groups):
    """
    setup_user_devices authenticates the user and creates devices
    attached to (static) groups given by the proportion map from
    the groups parameter.
    :param users:     Users to setup devices for (list).
    :param n_devices: Number of accepted devices created for each
                      user (int).
    :param groups:    Map of group names to device proportions, the
                      sum of proportion must be less than or equal
                      to 1 (dict[str] = float)
    :return: Dict mapping group_name -> list(devices)
    """
    devauth_DEV = ApiClient(deviceauth.URL_DEVICES)
    devauth_MGMT = ApiClient(deviceauth.URL_MGMT)
    invtry_MGMT = ApiClient(inventory.URL_MGMT)
    user = tenant.users[0]
    grouped_devices = {}
    group = None

    login_tenant_users(tenant)

    tenant.devices = []
    for group, dev_cnt in device_groups.items():
        grouped_devices[group] = []
        for i in range(dev_cnt):
            device = make_accepted_device(
                devauth_DEV, devauth_MGMT, user.token, tenant.tenant_token
            )
            if group is not None:
                rsp = invtry_MGMT.with_auth(user.token).call(
                    "PUT",
                    inventory.URL_DEVICE_GROUP.format(id=device.id),
                    body={"group": group},
                )
                assert rsp.status_code == 204

            device.group = group
            grouped_devices[group].append(device)
            tenant.devices.append(device)

    return grouped_devices


def add_user_to_role(user, tenant, role):
    """
    Ensures that the role exists and adds the user to it.
    NOTE: if creating a role to limit deployable groups, make sure to add
          another role that permits usage of the API endpoint for creating
          deployments.
    :param user:   the user to which the roles should be added
                   (common.User)
    :param tenant: tenant for which the user belongs (common.Tenant)
                   NOTE: it is assumed that tenant.users[0] has no
                         RBAC restrictions and can create new roles.
    :param roles:  the (list) of roles to constrain the user
                   (UserRole or list{UserRole})
    :return: None
    """
    useradm_MGMT = ApiClient(useradm.URL_MGMT)
    roles = [role.name]
    admin_user = tenant.users[0]
    if getattr(admin_user, "token", None) is None:
        rsp = useradm_MGMT.call(
            "POST", useradm.URL_LOGIN, auth=(admin_user.name, admin_user.pwd)
        )
        assert rsp.status_code == 200, rsp.text
        admin_user.token = rsp.text

    rsp = useradm_MGMT.with_auth(admin_user.token).call(
        "GET", useradm.URL_USERS_ID.format(id=user.id)
    )
    assert rsp.status_code == 200
    roles.append(*rsp.json()["roles"])

    rsp = useradm_MGMT.with_auth(admin_user.token).call(
        "POST", useradm.URL_ROLES, role.dict
    )
    assert rsp.status_code == 201

    rsp = useradm_MGMT.with_auth(admin_user.token).call(
        "PUT", useradm.URL_USERS_ID.format(id=user.id), body={"roles": roles},
    )
    assert rsp.status_code == 204, rsp.text


class TestRBACDeviceGroupEnterprise:
    @property
    def logger(self):
        return logging.getLogger(self.__class__.__name__)

    @pytest.mark.parametrize(
        "test_case",
        [
            {
                "name": "Test RBAC deploy to device group",
                "user": {"name": "test1-UUID@example.com", "pwd": "password"},
                "permissions": [
                    UserPermission("CREATE_DEPLOYMENT", "DEVICE_GROUP", "test")
                ],
                "device_groups": {"test": 5, "production": 30, "staging": 20},
                "deploy_groups": ["test"],
                "status_code": 201,
            },
            {
                "name": "Test RBAC deployment forbidden",
                "user": {"name": "test2-UUID@example.com", "pwd": "password"},
                "permissions": [
                    UserPermission("CREATE_DEPLOYMENT", "DEVICE_GROUP", "test")
                ],
                "device_groups": {"test": 5, "production": 25},
                "deploy_groups": ["production"],
                "status_code": 405,
            },
            # { TODO: This test-case should pass, but deployments only
            #         expect device IDs to come from the same group
            #   "name": "Test RBAC deployment multiple device groups",
            #   "user": {"name": "test3-UUID@example.com", "pwd": "password"},
            #   "permissions": [
            #       UserPermission("CREATE_DEPLOYMENT", "DEVICE_GROUP", "test"),
            #       UserPermission("CREATE_DEPLOYMENT", "DEVICE_GROUP", "staging"),
            #   ],
            #   "device_groups": {"test": 5, "staging": 15, "production": 25},
            #   "deploy_groups": ["test", "staging"],
            #   "status_code": 201,
            # },
            {
                "name": "Test RBAC deploy to devices outside group",
                "user": {"name": "test4-UUID@example.com", "pwd": "password"},
                "permissions": [
                    UserPermission("CREATE_DEPLOYMENT", "DEVICE_GROUP", "test"),
                ],
                "device_groups": {"test": 5, "staging": 15, "production": 25},
                "deploy_groups": ["test", "staging"],
                "status_code": 405,
            },
        ],
    )
    def test_deploy_to_group(self, clean_mongo, test_case):
        """
        Tests adding group restrinction to roles and checking that users
        are not allowed to deploy to devices outside the restricted
        groups.
        """
        self.logger.info("RUN: %s", test_case["name"])

        uuidv4 = str(uuid.uuid4())
        tenant, username, password = (
            "test.mender.io-" + uuidv4,
            "some.user+" + uuidv4 + "@example.com",
            "secretsecret",
        )
        tenant = create_org(tenant, username, password, "enterprise")
        test_case["user"]["name"] = test_case["user"]["name"].replace("UUID", uuidv4)
        test_user = create_user(tid=tenant.id, **test_case["user"])
        tenant.users.append(test_user)
        login_tenant_users(tenant)

        # Initialize tenant's devices
        grouped_devices = setup_tenant_devices(tenant, test_case["device_groups"])

        # Add user to deployment group
        role = UserRole("RBAC_DEVGRP", test_case["permissions"])
        add_user_to_role(test_user, tenant, role)

        # Upload a bogus artifact
        artifact = Artifact("tester", ["qemux86-64"], payload="bogus")

        dplmnt_MGMT = ApiClient(deployments.URL_MGMT)
        rsp = dplmnt_MGMT.with_auth(test_user.token).call(
            "POST",
            deployments.URL_DEPLOYMENTS_ARTIFACTS,
            files=(
                (
                    "artifact",
                    ("artifact.mender", artifact.make(), "application/octet-stream",),
                ),
            ),
        )
        assert rsp.status_code == 201, rsp.text

        # Attempt to create deployment with test user
        devices = []
        for group in test_case["deploy_groups"]:
            for device in grouped_devices[group]:
                devices.append(device.id)

        rsp = dplmnt_MGMT.with_auth(test_user.token).call(
            "POST",
            deployments.URL_DEPLOYMENTS,
            body={"artifact_name": "tester", "name": "dplmnt", "devices": devices},
        )
        assert rsp.status_code == test_case["status_code"], rsp.text
        self.logger.info("PASS: %s" % test_case["name"])

    @pytest.mark.parametrize(
        "test_case",
        [
            {
                "name": "Test RBAC deploy configuration to device belonging to a given group",
                "user": {"name": "test1-UUID@example.com", "pwd": "password"},
                "permissions": [
                    UserPermission("CREATE_DEPLOYMENT", "DEVICE_GROUP", "test")
                ],
                "device_groups": {"test": 1, "production": 1},
                "deploy_group": "test",
                "set_configuration_status_code": 204,
                "deploy_configuration_status_code": 200,
            },
            {
                "name": "Test RBAC configuration deployment forbidden",
                "user": {"name": "test2-UUID@example.com", "pwd": "password"},
                "permissions": [
                    UserPermission("CREATE_DEPLOYMENT", "DEVICE_GROUP", "test")
                ],
                "device_groups": {"test": 1, "production": 1},
                "deploy_group": "production",
                "set_configuration_status_code": 403,
                "deploy_configuration_status_code": 403,
            },
        ],
    )
    def test_set_and_deploy_configuration(self, clean_mongo, test_case):
        """
        Tests adding group restrinction to roles and checking that users
        are not allowed to set and deploy configuration to devices outside the restricted
        groups.
        """
        self.logger.info("RUN: %s", test_case["name"])

        uuidv4 = str(uuid.uuid4())
        tenant, username, password = (
            "test.mender.io-" + uuidv4,
            "some.user+" + uuidv4 + "@example.com",
            "secretsecret",
        )
        tenant = create_org(tenant, username, password, "enterprise")

        update_tenant(tenant.id, addons=["configure"])

        test_case["user"]["name"] = test_case["user"]["name"].replace("UUID", uuidv4)
        test_user = create_user(tid=tenant.id, **test_case["user"])
        tenant.users.append(test_user)
        login_tenant_users(tenant)

        # Initialize tenant's devices
        grouped_devices = setup_tenant_devices(tenant, test_case["device_groups"])

        # Add user to deployment group
        role = UserRole("RBAC_DEVGRP", test_case["permissions"])
        add_user_to_role(test_user, tenant, role)

        deviceconf_MGMT = ApiClient(deviceconfig.URL_MGMT)

        device_id = grouped_devices[test_case["deploy_group"]][0].id

        # Attempt to set configuration
        rsp = deviceconf_MGMT.with_auth(test_user.token).call(
            "PUT",
            deviceconfig.URL_MGMT_DEVICE_CONFIGURATION.format(id=device_id),
            body={"foo": "bar"},
        )
        assert rsp.status_code == test_case["set_configuration_status_code"], rsp.text

        # Attempt to deploy the configuration
        rsp = deviceconf_MGMT.with_auth(test_user.token).call(
            "POST",
            deviceconfig.URL_MGMT_DEVICE_CONFIGURATION_DEPLOY.format(id=device_id),
            body={"retries": 0},
        )
        assert (
            rsp.status_code == test_case["deploy_configuration_status_code"]
        ), rsp.text
        self.logger.info("PASS: %s" % test_case["name"])

    @pytest.mark.parametrize(
        "test_case",
        [
            {
                "name": "Test RBAC deploy configuration to device belonging to a given group",
                "user": {"name": "test1-UUID@example.com", "pwd": "password"},
                "permissions": [UserPermission("VIEW_DEVICE", "DEVICE_GROUP", "test")],
                "device_groups": {"test": 1, "production": 1},
                "view_group": "test",
                "get_configuration_status_code": 200,
            },
            {
                "name": "Test RBAC configuration deployment forbidden",
                "user": {"name": "test2-UUID@example.com", "pwd": "password"},
                "permissions": [UserPermission("VIEW_DEVICE", "DEVICE_GROUP", "test")],
                "device_groups": {"test": 1, "production": 1},
                "view_group": "production",
                "get_configuration_status_code": 403,
            },
        ],
    )
    def test_get_configuration(self, clean_mongo, test_case):
        """
        Tests adding group restrinction to roles and checking that users
        are not allowed to set and deploy configuration to devices outside the restricted
        groups.
        """
        self.logger.info("RUN: %s", test_case["name"])

        uuidv4 = str(uuid.uuid4())
        tenant, username, password = (
            "test.mender.io-" + uuidv4,
            "some.user+" + uuidv4 + "@example.com",
            "secretsecret",
        )
        tenant = create_org(tenant, username, password, "enterprise")
        update_tenant(tenant.id, addons=["configure"])

        admin_user = tenant.users[0]
        test_case["user"]["name"] = test_case["user"]["name"].replace("UUID", uuidv4)
        test_user = create_user(tid=tenant.id, **test_case["user"])
        tenant.users.append(test_user)
        login_tenant_users(tenant)

        # Initialize tenant's devices
        grouped_devices = setup_tenant_devices(tenant, test_case["device_groups"])

        # Add user to deployment group
        role = UserRole("RBAC_DEVGRP", test_case["permissions"])
        add_user_to_role(test_user, tenant, role)

        deviceconf_MGMT = ApiClient(deviceconfig.URL_MGMT)

        device_id = grouped_devices[test_case["view_group"]][0].id

        # set the configuration using admin account
        rsp = deviceconf_MGMT.with_auth(admin_user.token).call(
            "PUT",
            deviceconfig.URL_MGMT_DEVICE_CONFIGURATION.format(id=device_id),
            body={"foo": "bar"},
        )
        assert rsp.status_code == 204, rsp.text

        # Attempt to get configuration
        rsp = deviceconf_MGMT.with_auth(test_user.token).call(
            "GET", deviceconfig.URL_MGMT_DEVICE_CONFIGURATION.format(id=device_id),
        )
        assert rsp.status_code == test_case["get_configuration_status_code"], rsp.text
        self.logger.info("PASS: %s" % test_case["name"])
