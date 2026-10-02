"""Constants for the EU Omoda / Jaecoo integration."""

DOMAIN = "omoda_jaecoo"
CONF_EMAIL = "email"
CONF_PHONE = "phone"
CONF_ACCOUNT_TYPE = "account_type"
CONF_AUTH_METHOD = "auth_method"
CONF_OTP = "otp"
CONF_OTP_ACTION = "otp_action"
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 3
CONF_COUNTRY_CODE = "country_code"
CONF_TOKENS = "tokens"
CONF_VEHICLES = "vehicles"
CONF_SELECTED_VINS = "selected_vins"
CONF_CONTROL_PIN = "control_pin"
CONF_CLEAR_PIN = "clear_pin"
CONF_ENABLE_CONTROLS = "enable_controls"
CONF_PIN_BLOCKED = "pin_blocked"
CONF_CLIMATE_DURATION = "climate_duration"
DEFAULT_CLIMATE_DURATION = 15
COMMAND_COOLDOWN_SECONDS = 30
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL = 5  # Minutes; passive cloud reads, never wake commands.
MIN_POLL_INTERVAL = 5
MAX_POLL_INTERVAL = 60
STALE_AFTER_SECONDS = 15 * 60
