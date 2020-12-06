import time
from base64 import b32encode
from binascii import unhexlify
from urllib.parse import quote, urlencode

from django.db import models
from django.utils.module_loading import import_string
from django.template.loader import get_template
from django.template import TemplateDoesNotExist, TemplateSyntaxError
from django.utils.translation import gettext_lazy as _
from django.conf import settings

from django_otp.models import ThrottlingMixin
from django_otp.util import hex_validator, random_hex
from django_otp.oath import TOTP

from .abstract import AbstractDevice, AbstractUserMixin


# reserved 
# try:
#     AuthenticatorProviderPath = settings.MULTAUTH_DEVICE_AUTHENTICATOR_PROVIDER
#     AuthenticatorProvider = import_string(AuthenticatorProviderPath) # ex. multauth.providers.GauthenticatorProvider
# except AttributeError:
#     from ..providers.mail import GauthenticatorProvider
#     AuthenticatorProvider = GauthenticatorProvider


DEBUG = getattr(settings, 'DEBUG', False)
MULTAUTH_DEBUG = getattr(settings, 'MULTAUTH_DEBUG', DEBUG)
MULTAUTH_KEY_LENGTH = getattr(settings, 'MULTAUTH_DEVICE_AUTHENTICATOR_KEY_LENGTH', 8)
MULTAUTH_SYNC = getattr(settings, 'MULTAUTH_DEVICE_AUTHENTICATOR_SYNC', True)
MULTAUTH_THROTTLE_FACTOR = getattr(settings, 'MULTAUTH_DEVICE_AUTHENTICATOR_THROTTLE_FACTOR', 1)
MULTAUTH_ISSUER = getattr(settings, 'MULTAUTH_DEVICE_AUTHENTICATOR_ISSUER', None)


# see django_otp.plugins.otp_totp.models.TOTPDevice
def default_key():
    return random_hex(MULTAUTH_KEY_LENGTH)


# see django_otp.plugins.otp_totp.models.TOTPDevice
def key_validator(value):
    return hex_validator()(value)


class AuthenticatorDevice(ThrottlingMixin, AbstractDevice):
    # see django_otp.plugins.otp_totp.models.TOTPDevice
    key = models.CharField(max_length=80, validators=[key_validator], default=default_key) # a hex-encoded secret key of up to 40 bytes
    step = models.PositiveSmallIntegerField(default=30) # the time step in seconds.
    t0 = models.BigIntegerField(default=0) # the Unix time at which to begin counting steps
    digits = models.PositiveSmallIntegerField(choices=[(6, 6), (8, 8)], default=6) # the number of digits to expect in a token
    tolerance = models.PositiveSmallIntegerField(default=1) # the number of time steps in the past or future to allow
    drift = models.SmallIntegerField(default=0) # the number of time steps the prover is known to deviate from our clock
    last_t = models.BigIntegerField(default=-1) # the t value of the latest verified token. The next token must be at a higher time step

    USER_MIXIN = 'AuthenticatorUserMixin'

    def __eq__(self, other):
        if not isinstance(other, AuthenticatorDevice):
            return False

        return self.key == other.key

    # useless?
    def __hash__(self):
        return hash((self.key,))

    def clean(self):
        super().clean()

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = self.generate_key()
        return super().save(*args, **kwargs)

    def generate_totp(self, request=None):
        key = self.bin_key
        totp = TOTP(key, self.step, self.t0, self.digits, self.drift)
        totp.time = time.time()
        return totp

    def generate_challenge(self, request=None):
        totp = self.generate_totp()

        if MULTAUTH_DEBUG:
            print('Fake auth message, Google Authenticator, token: %s ' % (totp))

        return totp

    # see django_otp.plugins.otp_totp.models.TOTPDevice
    @property
    def bin_key(self):
        return unhexlify(self.key.encode())

    # see django_otp.plugins.otp_totp.models.TOTPDevice
    @property
    def key_uri(self):
        issuer = MULTAUTH_ISSUER or self._meta.app_label
        params = {
            'secret': b32encode(self.bin_key),
            'algorithm': 'SHA1',
            'digits': self.digits,
            'period': self.step,
        }
        urlencoded_params = urlencode(params)

        if callable(issuer):
            issuer = issuer(self)

        if isinstance(issuer, str) and (issuer != ''):
            issuer = issuer.replace(':', '')
            label = '{}:{}'.format(issuer, str(self.user))
            urlencoded_params += '&issuer={}'.format(quote(issuer))  # encode issuer as per RFC 3986, not quote_plus

        url = 'otpauth://totp/{}?{}'.format(quote(label), urlencoded_params)
        return url

    # see django_otp.plugins.otp_totp.models.TOTPDevice
    def verify_token(self, token):
        verify_allowed, _ = self.verify_is_allowed()
        if not verify_allowed:
            return False

        try:
            token = int(token)
        except Exception:
            verified = False
        else:
            totp = generate_totp()
            verified = totp.verify(token, self.tolerance, self.last_t + 1)
 
            if verified:
                self.last_t = totp.t()
                if MULTAUTH_THROTTLE_SYNC:
                    self.drift = totp.drift
                self.throttle_reset(commit=False)
                self.save()

        if not verified:
            self.throttle_increment(commit=True)

        return verified

    # see django_otp.plugins.otp_totp.models.TOTPDevice
    # see django_otp.models.ThrottlingMixin
    def get_throttle_factor(self):
        return MULTAUTH_THROTTLE_FACTOR


class AuthenticatorUserMixin(AbstractUserMixin):
    class Meta:
        abstract = True

    def __str__(self):
        return str(getattr(self, 'authenticator', ''))

    @property
    def is_authenticator_confirmed(self):
        return True

    def get_authenticator_device(self):
        try:
            device = AuthenticatorDevice.objects.get(user=self)
        except AuthenticatorDevice.DoesNotExist:
            device = None

        return device

    def verify_authenticator_token(self, token):
        device = self.get_authenticator_device()

        if not device:
            return False

        return device.verify_token(token) if token else False

    def verify(self, request=None):
        super().verify(request)
