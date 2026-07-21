"""Concrete SSO identity providers (vendor adapters).

The vendor SDK import lives inside each adapter's ``from_config`` constructor so
these modules stay import-safe where the SDK is absent (standalone). The DI
factory imports an adapter only when SSO is configured (cloud/oauth).
"""
