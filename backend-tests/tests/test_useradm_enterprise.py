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
import pytest
import base64
import io
import uuid
from urllib import parse
import re
import time

from PIL import Image
from pyzbar.pyzbar import decode
import pyotp

from testutils.infra.smtpd_mock import smtp_mock
from testutils.api.client import ApiClient
from testutils.infra.cli import CliUseradm, CliTenantadm
from testutils.infra.container_manager.kubernetes_manager import isK8S
import testutils.api.useradm as useradm
import testutils.api.tenantadm as tenantadm
from testutils.common import mongo, clean_mongo, create_org, create_user

uadm = ApiClient(useradm.URL_MGMT)


@pytest.fixture(scope="function")
def clean_migrated_mongo(clean_mongo):
    useradm_cli = CliUseradm()
    tenantadm_cli = CliTenantadm()

    for t in ["tenant1", "tenant2"]:
        useradm_cli.migrate(t)
        tenantadm_cli.migrate()

    yield clean_mongo


@pytest.fixture(scope="function")
def tenants_users(clean_migrated_mongo):
    tenants = []
    for n in range(2):
        uuidv4 = str(uuid.uuid4())
        tenant, username, password = (
            "test.mender.io-" + uuidv4,
            "some.user+" + uuidv4 + "@foo.com",
            "secretsecret",
        )
        # Create tenant with two users
        tenant = create_org(tenant, username, password, "enterprise")
        tenant.users.append(
            create_user("some.other.user+" + uuidv4 + "@foo.com", password, tenant.id)
        )
        tenants.append(tenant)

    yield tenants


@pytest.mark.skipif(
    isK8S(), reason="not testable in a staging or production environment"
)
class Test2FAEnterprise:
    def _login(self, user, totp=None):
        body = {}
        if totp is not None:
            body = {"token2fa": totp}

        r = uadm.call("POST", useradm.URL_LOGIN, auth=(user.name, user.pwd), body=body)
        return r

    def _verify(self, utoken, totp):
        body = {"token2fa": totp}

        r = uadm.with_auth(utoken).call("PUT", useradm.URL_2FAVERIFY, body=body)
        return r

    def _toggle_tfa(self, utoken, user_id, on=True):
        body = {"2fa": "enabled", user_id + "_2fa": "enabled"}
        if not on:
            body = {"2fa": "disabled", user_id + "_2fa": "disabled"}

        r = uadm.with_auth(utoken).call("POST", useradm.URL_SETTINGS, body)
        assert r.status_code == 201

    def _qr_dec(self, qr_b64):
        # decode png from temp inmem file
        b = base64.b64decode(qr_b64)
        f = io.BytesIO(b)
        image = Image.open(f)

        # decode qr code - results in a otpauth://... url in 'data' bytes
        dec = decode(image)[0]

        qs = parse.urlsplit(dec.data).query

        secret_b32 = parse.parse_qs(qs)[b"secret"][0]

        return secret_b32

    def test_enable_disable(self, tenants_users, smtp_mock):
        user_2fa = tenants_users[0].users[0]
        user_no_2fa = tenants_users[0].users[1]

        r = self._login(user_2fa)
        assert r.status_code == 200
        user_2fa_tok = r.text

        # verify user email address
        r = uadm.post(useradm.URL_VERIFY_EMAIL_START, body={"email": user_2fa.name})
        assert r.status_code == 202
        # wait for the verification email
        message = None
        for i in range(15):
            messages = smtp_mock.filtered_messages(user_2fa.name)
            if len(messages) > 0:
                message = messages[0]
                break
            time.sleep(1)
        # be sure we received the email
        assert message is not None
        assert message.data != ""
        # extract the secret hash from the link
        match = re.search(
            r"https://hosted.mender.io/ui/#/activate/([a-z0-9\-]+)",
            message.data.decode("utf-8"),
        )
        secret_hash = match.group(1)
        assert secret_hash != ""
        # complete the email address
        r = uadm.post(
            useradm.URL_VERIFY_EMAIL_COMPLETE, body={"secret_hash": secret_hash}
        )
        assert r.status_code == 204

        # enable tfa for 1 user, straight login still works, token is not verified
        self._toggle_tfa(user_2fa_tok, user_2fa.id, on=True)
        r = self._login(user_2fa)
        assert r.status_code == 200

        # grab qr code, extract token, calc TOTP
        r = uadm.with_auth(user_2fa_tok).call("GET", useradm.URL_2FAQR)

        assert r.status_code == 200

        secret = self._qr_dec(r.json()["qr"])
        totp = pyotp.TOTP(secret)
        tok = totp.now()

        # verify token
        r = self._verify(user_2fa_tok, tok)
        assert r.status_code == 202

        # login with totp succeeds
        r = self._login(user_2fa, totp=tok)
        assert r.status_code == 200

        # logi without otp now does not work
        r = self._login(user_2fa)
        assert r.status_code == 401

        # the other user, and other tenant's users, are unaffected
        r = self._login(user_no_2fa)
        assert r.status_code == 200

        for other_user in tenants_users[1].users:
            r = self._login(other_user)
            assert r.status_code == 200

        # after disabling - straight login works again
        self._toggle_tfa(user_2fa_tok, user_2fa.id, on=False)
        r = self._login(user_2fa)
        assert r.status_code == 200
