import os
import unittest
from unittest.mock import patch

from elengtis.settings import Settings, SettingsError
from elengtis.observer import Observer


class SettingsTests(unittest.TestCase):
    def test_production_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SettingsError): Settings.from_environment()

    def test_public_url_must_be_https(self):
        values = {
            'ELENGTIS_DATABASE_URL': 'postgresql://example', 'ELENGTIS_S3_ENDPOINT': 'http://minio',
            'ELENGTIS_S3_BUCKET': 'bucket', 'ELENGTIS_S3_ACCESS_KEY': 'access', 'ELENGTIS_S3_SECRET_KEY': 'secret',
            'ELENGTIS_PUBLIC_URL': 'http://localhost', 'ELENGTIS_OIDC_ISSUER': 'https://issuer',
            'ELENGTIS_OIDC_CLIENT_ID': 'client', 'ELENGTIS_OIDC_CLIENT_SECRET': 'secret',
            'ELENGTIS_SESSION_SECRET': 'long-random-secret', 'ELENGTIS_TRUSTED_PROXY_IPS': '127.0.0.1',
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaises(SettingsError): Settings.from_environment()


class ObserverTests(unittest.TestCase):
    def test_default_observer_is_non_cancelling(self):
        self.assertFalse(Observer().cancelled())


if __name__ == '__main__': unittest.main()
