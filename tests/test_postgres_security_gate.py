import os
import unittest
from unittest.mock import patch
from app.security import DEV_AUTH_SECRET, auth_cookie_attributes, get_auth_cookie_secret, security_gate_facts, validate_auth_secret
from app import server
STRONG_SECRET = "stage-70a-auth-secret-with-more-than-32-characters"
class PostgresSecurityGateTests(unittest.TestCase):
 def test_dev_cookie_policy(self):
  self.assertEqual(get_auth_cookie_secret({}),DEV_AUTH_SECRET); self.assertEqual(validate_auth_secret({}),[])
  self.assertEqual(auth_cookie_attributes({})["SameSite"],"Lax")
 def test_production_requires_strong_secret(self):
  for secret in (None,"secret","changeme",DEV_AUTH_SECRET,"too-short"):
   env={"MVP_PRODUCTION_SECURITY":"1"}
   if secret is not None: env["MVP_AUTH_SECRET"]=secret
   with self.subTest(secret=secret): self.assertTrue(validate_auth_secret(env))
  self.assertEqual(get_auth_cookie_secret({"MVP_PRODUCTION_SECURITY":"1","MVP_AUTH_SECRET":STRONG_SECRET}),STRONG_SECRET)
 def test_production_cookie_is_secure(self):
  with patch.dict(os.environ,{"MVP_PRODUCTION_SECURITY":"1","MVP_AUTH_SECRET":STRONG_SECRET},clear=True): header=server.auth_cookie_header(7)[1]
  for flag in ("Secure","HttpOnly","SameSite=Lax","Path=/"): self.assertIn(flag,header)
  self.assertNotIn(STRONG_SECRET,header)
 def test_passwordless_switching_forbidden(self):
  self.assertFalse(security_gate_facts({"MVP_PRODUCTION_SECURITY":"1"})["passwordless_user_switching_allowed"])
