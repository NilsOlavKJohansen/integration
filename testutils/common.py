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
import pytest
import random
import time
import string
import tempfile
import os
import subprocess
import redo
import requests
from contextlib import contextmanager

import testutils.api.deviceauth as deviceauth
import testutils.api.tenantadm as tenantadm
import testutils.api.useradm as useradm
import testutils.util.crypto
from testutils.api.client import ApiClient, GATEWAY_HOSTNAME
from testutils.infra.container_manager.kubernetes_manager import isK8S
from testutils.infra.mongo import MongoClient
from testutils.infra.cli import CliUseradm, CliTenantadm


@pytest.fixture(scope="session")
def mongo():
    return MongoClient("mender-mongo:27017")


@pytest.fixture(scope="function")
def clean_mongo(mongo):
    """Fixture setting up a clean (i.e. empty database). Yields
    pymongo.MongoClient connected to the DB."""
    mongo_cleanup(mongo)
    yield mongo.client
    mongo_cleanup(mongo)


def mongo_cleanup(mongo):
    mongo.cleanup()


class User:
    def __init__(self, id, name, pwd):
        self.name = name
        self.pwd = pwd
        self.id = id


class Authset:
    def __init__(self, id, did, id_data, pubkey, privkey, status):
        self.id = id
        self.did = did
        self.id_data = id_data
        self.pubkey = pubkey
        self.privkey = privkey
        self.status = status


class Device:
    def __init__(self, id, id_data, pubkey, tenant_token=""):
        self.id = id
        self.id_data = id_data
        self.pubkey = pubkey
        self.tenant_token = tenant_token
        self.authsets = []
        self.token = None


class Tenant:
    def __init__(self, name, id, token):
        self.name = name
        self.users = []
        self.devices = []
        self.id = id
        self.tenant_token = token


def create_random_authset(dauthd1, dauthm, utoken, tenant_token=""):
    """ create_device with random id data and keypair"""
    priv, pub = testutils.util.crypto.get_keypair_rsa()
    mac = ":".join(["{:02x}".format(random.randint(0x00, 0xFF), "x") for i in range(6)])
    id_data = {"mac": mac}

    return create_authset(dauthd1, dauthm, id_data, pub, priv, utoken, tenant_token)


def create_authset(dauthd1, dauthm, id_data, pubkey, privkey, utoken, tenant_token=""):
    body, sighdr = deviceauth.auth_req(id_data, pubkey, privkey, tenant_token)

    # submit auth req
    r = dauthd1.call("POST", deviceauth.URL_AUTH_REQS, body, headers=sighdr)
    assert r.status_code == 401, r.text

    # dev must exist and have *this* aset
    api_dev = get_device_by_id_data(dauthm, id_data, utoken)
    assert api_dev is not None

    aset = [
        a
        for a in api_dev["auth_sets"]
        if testutils.util.crypto.compare_keys(a["pubkey"], pubkey)
    ]
    assert len(aset) == 1, str(aset)

    aset = aset[0]

    assert aset["identity_data"] == id_data
    assert aset["status"] == "pending"

    return Authset(aset["id"], api_dev["id"], id_data, pubkey, privkey, "pending")


def create_user(name, pwd, tid="", containers_namespace="backend-tests"):
    cli = CliUseradm(containers_namespace)

    uid = cli.create_user(name, pwd, tid)

    return User(uid, name, pwd)


def create_org(
    name,
    username,
    password,
    plan="os",
    containers_namespace="backend-tests",
    container_manager=None,
):
    cli = CliTenantadm(
        containers_namespace=containers_namespace, container_manager=container_manager
    )
    user_id = None
    tenant_id = cli.create_org(name, username, password, plan=plan)
    tenant_token = json.loads(cli.get_tenant(tenant_id))["tenant_token"]

    host = GATEWAY_HOSTNAME
    if container_manager is not None:
        host = container_manager.get_mender_gateway()
    api = ApiClient(useradm.URL_MGMT, host=host)

    # Try log in every second for 3 minutes.
    # - There usually is a slight delay (in order of ms) for propagating
    #   the created user to the db.
    for i in range(3 * 60):
        rsp = api.call("POST", useradm.URL_LOGIN, auth=(username, password))
        if rsp.status_code == 200:
            break
        time.sleep(1)

    if rsp.status_code != 200:
        raise ValueError(
            "User could not log in within three minutes after organization has been created."
        )

    user_token = rsp.text
    rsp = api.with_auth(user_token).call("GET", useradm.URL_USERS)
    users = json.loads(rsp.text)
    for user in users:
        if user["email"] == username:
            user_id = user["id"]
            break
    if user_id == None:
        raise ValueError("Error retrieving user id.")

    tenant = Tenant(name, tenant_id, tenant_token)
    user = User(user_id, username, password)
    tenant.users.append(user)
    return tenant


def get_device_by_id_data(dauthm, id_data, utoken):
    page = 0
    per_page = 20
    qs_params = {}
    found = None
    while True:
        page = page + 1
        qs_params["page"] = page
        qs_params["per_page"] = per_page
        r = dauthm.with_auth(utoken).call(
            "GET", deviceauth.URL_MGMT_DEVICES, qs_params=qs_params
        )
        assert r.status_code == 200
        api_devs = r.json()

        found = [d for d in api_devs if d["identity_data"] == id_data]
        if len(found) > 0:
            break

        if len(api_devs) == 0:
            break

    assert len(found) == 1, "device not found by id data"

    return found[0]


def change_authset_status(dauthm, did, aid, status, utoken):
    r = dauthm.with_auth(utoken).call(
        "PUT",
        deviceauth.URL_AUTHSET_STATUS,
        deviceauth.req_status(status),
        path_params={"did": did, "aid": aid},
    )
    assert r.status_code == 204


def rand_id_data():
    mac = ":".join(["{:02x}".format(random.randint(0x00, 0xFF), "x") for i in range(6)])
    sn = "".join(["{}".format(random.randint(0x00, 0xFF)) for i in range(6)])

    return {"mac": mac, "sn": sn}


def make_pending_device(dauthd1, dauthm, utoken, tenant_token=""):
    id_data = rand_id_data()

    priv, pub = testutils.util.crypto.get_keypair_rsa()
    new_set = create_authset(
        dauthd1, dauthm, id_data, pub, priv, utoken, tenant_token=tenant_token
    )

    dev = Device(new_set.did, new_set.id_data, pub, tenant_token)

    dev.authsets.append(new_set)

    dev.status = "pending"

    return dev


def make_accepted_device(dauthd1, dauthm, utoken, tenant_token=""):
    dev = make_pending_device(dauthd1, dauthm, utoken, tenant_token=tenant_token)
    aset_id = dev.authsets[0].id
    change_authset_status(dauthm, dev.id, aset_id, "accepted", utoken)

    aset = dev.authsets[0]
    aset.status = "accepted"

    # obtain auth token
    body, sighdr = deviceauth.auth_req(
        aset.id_data, aset.pubkey, aset.privkey, tenant_token
    )

    r = dauthd1.call("POST", deviceauth.URL_AUTH_REQS, body, headers=sighdr)

    assert r.status_code == 200
    dev.token = r.text

    dev.status = "accepted"

    return dev


def make_accepted_devices(devauthd, devauthm, utoken, tenant_token="", num_devices=1):
    """Create accepted devices.
    returns list of Device objects."""
    devices = []

    # some 'accepted' devices, single authset
    for _ in range(num_devices):
        dev = make_accepted_device(devauthd, devauthm, utoken, tenant_token)
        devices.append(dev)

    return devices


@contextmanager
def get_mender_artifact(
    artifact_name="test",
    update_module="dummy",
    device_types=("arm1",),
    size=256,
    depends=(),
    provides=(),
):
    data = "".join(random.choices(string.ascii_uppercase + string.digits, k=size))
    f = tempfile.NamedTemporaryFile(delete=False)
    f.write(data.encode("utf-8"))
    f.close()
    #
    filename = f.name
    artifact = "%s.mender" % filename
    args = [
        "mender-artifact",
        "write",
        "module-image",
        "-o",
        artifact,
        "--artifact-name",
        artifact_name,
        "-T",
        update_module,
        "-f",
        filename,
    ]
    for device_type in device_types:
        args.extend(["-t", device_type])
    for depend in depends:
        args.extend(["--depends", depend])
    for provide in provides:
        args.extend(["--provides", provide])
    try:
        subprocess.call(args)
        yield artifact
    finally:
        os.unlink(filename)
        os.path.exists(artifact) and os.unlink(artifact)


def wait_for_traefik(gateway_host, routers=[]):
    """Wait until provided routers are installed.
    Prevents race conditions where services are already up but traefik hasn't yet registered their routers. This causes subtle timing issues.
    By default checks the basic routers (incl. deployments - startup so time consuming, in practice it guarantees success).
    """
    if isK8S():
        return
    if routers == []:
        rnames = [
            "deployments@docker",
            "deploymentsMgmt@docker",
            "minio@docker",
            "deviceauth@docker",
            "deviceauthMgmt@docker",
            "inventoryMgmt@docker",
            "inventoryMgmtV1@docker",
            "useradm@docker",
            "useradmLogin@docker",
            "deviceauth@docker",
            "deviceauthMgmt@docker",
            "inventoryV1@docker",
        ]
    else:
        rnames = routers[:]

    for _ in redo.retrier(attempts=5, sleeptime=10):
        try:
            r = requests.get("http://{}:8080/api/http/routers".format(gateway_host))
            assert r.status_code == 200

            cur_routers = [x["name"] for x in r.json()]

            if set(cur_routers).issuperset(set(rnames)):
                break
        except requests.exceptions.ConnectionError as ex:
            print("connection error while waiting for routers - but that's ok")
    else:
        assert False, "timeout hit waiting for traefik routers {}".format(rnames)


def update_tenant(tid, addons=None, plan=None, container_manager=None):
    """ Call internal PUT tenantadm/tenants/{tid} """
    host = tenantadm.HOST
    if container_manager is not None:
        host = container_manager.get_ip_of_service("mender-tenantadm")[0] + ":8080"

    update = {}
    if addons is not None:
        update["addons"] = tenantadm.make_addons(addons)

    if plan is not None:
        update["plan"] = plan

    tadm = ApiClient(tenantadm.URL_INTERNAL, host=host, schema="http://")
    res = tadm.call(
        "PUT", tenantadm.URL_INTERNAL_TENANT, body=update, path_params={"tid": tid},
    )
    assert res.status_code == 202
