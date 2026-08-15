from __future__ import annotations

from functools import cached_property, lru_cache
from pathlib import Path
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
OFFICIAL_RENDER_API_ORIGIN = "https://luxury-backend-xy9d.onrender.com"
OFFICIAL_RENDER_WS_ORIGIN = "wss://luxury-backend-xy9d.onrender.com"
OFFICIAL_PUBLIC_API_ORIGIN = "https://api.luxuryshoppings.com"
OFFICIAL_PUBLIC_WS_ORIGIN = "wss://api.luxuryshoppings.com"
OFFICIAL_FRONTEND_ORIGINS = {
    "https://luxuryshoppings.com",
    "https://www.luxuryshoppings.com",
}
FORBIDDEN_PUBLIC_ENDPOINT_MARKERS = (
    "localhost",
    "127.0.0.1",
    "10.0.2.2",
    "0.0.0.0",
    "supabase",
    ":4000",
    ":8011",
    ":8798",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = Field(alias="DATABASE_URL")
    database_migration_url: str = Field("", alias="DATABASE_MIGRATION_URL")
    app_env: str = Field("development", alias="APP_ENV")
    allow_test_fixtures: bool = Field(False, alias="ALLOW_TEST_FIXTURES")
    jwt_secret: str = Field(alias="JWT_SECRET", min_length=32)
    jwt_access_token_minutes: int = Field(30, alias="JWT_ACCESS_TOKEN_MINUTES", ge=5, le=1440)
    jwt_refresh_token_days: int = Field(30, alias="JWT_REFRESH_TOKEN_DAYS", ge=1, le=365)
    upload_dir: Path = Field(Path("backend/data/uploads"), alias="UPLOAD_DIR")
    upload_fallback_dir: Path | None = Field(None, alias="UPLOAD_FALLBACK_DIR")
    storage_provider: str = Field("local", alias="STORAGE_PROVIDER")
    render_public_url: str = Field(OFFICIAL_RENDER_API_ORIGIN, alias="RENDER_PUBLIC_URL")
    r2_endpoint_url: str = Field("", alias="R2_ENDPOINT_URL")
    r2_bucket: str = Field("", alias="R2_BUCKET")
    r2_access_key_id: str = Field("", alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field("", alias="R2_SECRET_ACCESS_KEY")
    r2_region: str = Field("auto", alias="R2_REGION")
    r2_public_base_url: str = Field("", alias="R2_PUBLIC_BASE_URL")
    api_base_url: str = Field("http://127.0.0.1:8000", alias="API_BASE_URL")
    app_public_url: str = Field("http://127.0.0.1:8000", alias="APP_PUBLIC_URL")
    frontend_public_url: str = Field("http://127.0.0.1:5190", alias="FRONTEND_PUBLIC_URL")
    flutter_reset_deep_link: str = Field("luxury://reset-password", alias="FLUTTER_RESET_DEEP_LINK")
    password_reset_allowed_redirects: str = Field("", alias="PASSWORD_RESET_ALLOWED_REDIRECTS")
    ws_base_url: str = Field("ws://127.0.0.1:8000", alias="WS_BASE_URL")
    cors_origins: str = Field("", alias="CORS_ORIGINS")
    backup_storage_dir: Path = Field(Path("backend/data/secure-backups"), alias="BACKUP_STORAGE_DIR")
    backup_offsite_dir: Path = Field(Path("backend/data/offsite-secure-backups"), alias="BACKUP_OFFSITE_DIR")
    backup_offsite_provider: str = Field("filesystem", alias="BACKUP_OFFSITE_PROVIDER")
    backup_s3_endpoint_url: str = Field("", alias="BACKUP_S3_ENDPOINT_URL")
    backup_s3_bucket: str = Field("", alias="BACKUP_S3_BUCKET")
    backup_s3_region: str = Field("", alias="BACKUP_S3_REGION")
    backup_s3_prefix: str = Field("luxury-secure-backups", alias="BACKUP_S3_PREFIX")
    backup_s3_access_key_id: str = Field("", alias="BACKUP_S3_ACCESS_KEY_ID")
    backup_s3_secret_access_key: str = Field("", alias="BACKUP_S3_SECRET_ACCESS_KEY")
    backup_s3_session_token: str = Field("", alias="BACKUP_S3_SESSION_TOKEN")
    backup_encryption_key_file: Path | None = Field(None, alias="BACKUP_ENCRYPTION_KEY_FILE")
    backup_pg_bin_dir: Path | None = Field(None, alias="BACKUP_PG_BIN_DIR")
    backup_retention_days: int = Field(14, alias="BACKUP_RETENTION_DAYS", ge=1, le=3650)
    backup_command_timeout_seconds: int = Field(120, alias="BACKUP_COMMAND_TIMEOUT_SECONDS", ge=5, le=3600)
    backup_require_restore_verification: bool = Field(True, alias="BACKUP_REQUIRE_RESTORE_VERIFICATION")
    realtime_allowed_origins: str = Field("", alias="REALTIME_ALLOWED_ORIGINS")
    realtime_redis_url: str = Field("", alias="REALTIME_REDIS_URL")
    realtime_namespace: str = Field("luxury", alias="REALTIME_NAMESPACE")
    realtime_ticket_ttl_seconds: int = Field(45, alias="REALTIME_TICKET_TTL_SECONDS", ge=5, le=300)
    realtime_event_retention_seconds: int = Field(86400, alias="REALTIME_EVENT_RETENTION_SECONDS", ge=60, le=2_592_000)
    realtime_max_message_bytes: int = Field(4096, alias="REALTIME_MAX_MESSAGE_BYTES", ge=128, le=65_536)
    realtime_max_connections_per_user: int = Field(4, alias="REALTIME_MAX_CONNECTIONS_PER_USER", ge=1, le=50)
    realtime_max_connections_per_ip: int = Field(40, alias="REALTIME_MAX_CONNECTIONS_PER_IP", ge=1, le=1000)
    realtime_max_subscriptions_per_connection: int = Field(12, alias="REALTIME_MAX_SUBSCRIPTIONS_PER_CONNECTION", ge=1, le=100)
    realtime_max_inbound_messages_per_minute: int = Field(60, alias="REALTIME_MAX_INBOUND_MESSAGES_PER_MINUTE", ge=1, le=1000)
    realtime_heartbeat_interval_seconds: int = Field(20, alias="REALTIME_HEARTBEAT_INTERVAL_SECONDS", ge=5, le=120)
    realtime_pong_timeout_seconds: int = Field(45, alias="REALTIME_PONG_TIMEOUT_SECONDS", ge=10, le=300)
    max_upload_bytes: int = Field(10 * 1024 * 1024, alias="MAX_UPLOAD_BYTES", ge=1024)
    malware_scanner_required: bool = Field(True, alias="MALWARE_SCANNER_REQUIRED")
    login_rate_limit: int = Field(8, alias="LOGIN_RATE_LIMIT", ge=1)
    registration_rate_limit: int = Field(10, alias="REGISTRATION_RATE_LIMIT", ge=1)
    password_reset_rate_limit: int = Field(4, alias="PASSWORD_RESET_RATE_LIMIT", ge=1)
    otp_rate_limit: int = Field(3, alias="OTP_RATE_LIMIT", ge=1)
    trusted_proxy_ips: str = Field("", alias="TRUSTED_PROXY_IPS")
    trusted_proxy_cidrs: str = Field("", alias="TRUSTED_PROXY_CIDRS")
    public_read_rate_limit: int = Field(600, alias="PUBLIC_READ_RATE_LIMIT", ge=1)
    authentication_rate_limit: int = Field(60, alias="AUTHENTICATION_RATE_LIMIT", ge=1)
    search_rate_limit_anon: int = Field(60, alias="SEARCH_RATE_LIMIT_ANON", ge=1)
    search_rate_limit_auth: int = Field(180, alias="SEARCH_RATE_LIMIT_AUTH", ge=1)
    search_max_query_length: int = Field(120, alias="SEARCH_MAX_QUERY_LENGTH", ge=1, le=1000)
    search_max_page_size: int = Field(50, alias="SEARCH_MAX_PAGE_SIZE", ge=1, le=500)
    search_max_filters: int = Field(8, alias="SEARCH_MAX_FILTERS", ge=1, le=100)
    upload_rate_limit: int = Field(20, alias="UPLOAD_RATE_LIMIT", ge=1)
    support_rate_limit: int = Field(12, alias="SUPPORT_RATE_LIMIT", ge=1)
    resource_rate_limit: int = Field(180, alias="RESOURCE_RATE_LIMIT", ge=1)
    resource_max_page_size: int = Field(100, alias="RESOURCE_MAX_PAGE_SIZE", ge=1, le=1000)
    resource_admin_max_page_size: int = Field(500, alias="RESOURCE_ADMIN_MAX_PAGE_SIZE", ge=1, le=2000)
    resource_max_filters: int = Field(10, alias="RESOURCE_MAX_FILTERS", ge=1, le=100)
    customer_write_rate_limit: int = Field(120, alias="CUSTOMER_WRITE_RATE_LIMIT", ge=1)
    merchant_write_rate_limit: int = Field(120, alias="MERCHANT_WRITE_RATE_LIMIT", ge=1)
    admin_write_rate_limit: int = Field(240, alias="ADMIN_WRITE_RATE_LIMIT", ge=1)
    finance_write_rate_limit: int = Field(120, alias="FINANCE_WRITE_RATE_LIMIT", ge=1)
    internal_worker_rate_limit: int = Field(600, alias="INTERNAL_WORKER_RATE_LIMIT", ge=1)
    internal_diagnostics_rate_limit: int = Field(120, alias="INTERNAL_DIAGNOSTICS_RATE_LIMIT", ge=1)
    api_max_request_bytes: int = Field(1024 * 1024, alias="API_MAX_REQUEST_BYTES", ge=1024)
    api_route_timeout_seconds: int = Field(20, alias="API_ROUTE_TIMEOUT_SECONDS", ge=1, le=300)
    captcha_required: bool = Field(False, alias="CAPTCHA_REQUIRED")
    captcha_secret: str = Field("", alias="CAPTCHA_SECRET")
    captcha_verify_url: str = Field("https://www.google.com/recaptcha/api/siteverify", alias="CAPTCHA_VERIFY_URL")
    smtp_host: str = Field("", alias="SMTP_HOST")
    smtp_port: int = Field(465, alias="SMTP_PORT")
    smtp_username: str = Field("", alias="SMTP_USERNAME")
    smtp_password: str = Field("", alias="SMTP_PASSWORD")
    smtp_from_email: str = Field("", alias="SMTP_FROM_EMAIL")
    email_provider: str = Field("smtp", alias="EMAIL_PROVIDER")
    resend_api_url: str = Field("https://api.resend.com/emails", alias="RESEND_API_URL")
    resend_api_key: str = Field("", alias="RESEND_API_KEY")
    resend_from_email: str = Field("", alias="RESEND_FROM_EMAIL")
    whatsapp_provider_url: str = Field("", alias="WHATSAPP_PROVIDER_URL")
    whatsapp_access_token: str = Field("", alias="WHATSAPP_ACCESS_TOKEN")
    ai_api_url: str = Field("", alias="AI_API_URL")
    ai_api_key: str = Field("", alias="AI_API_KEY")
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")
    google_api_key: str = Field("", alias="GOOGLE_API_KEY")
    ai_provider_name: str = Field("configured_ai_provider", alias="AI_PROVIDER_NAME")
    ai_default_model: str = Field("default", alias="AI_DEFAULT_MODEL")
    ai_model_allowlist: str = Field("default", alias="AI_MODEL_ALLOWLIST")
    image_ai_enhancement_enabled: bool = Field(True, alias="IMAGE_AI_ENHANCEMENT_ENABLED")
    image_ai_model: str = Field("gemini-2.5-flash-image", alias="IMAGE_AI_MODEL")
    image_ai_timeout_seconds: int = Field(30, alias="IMAGE_AI_TIMEOUT_SECONDS", ge=5, le=120)
    image_ai_max_input_bytes: int = Field(5 * 1024 * 1024, alias="IMAGE_AI_MAX_INPUT_BYTES", ge=64 * 1024, le=10 * 1024 * 1024)
    image_max_dimension: int = Field(2400, alias="IMAGE_MAX_DIMENSION", ge=512, le=4096)
    image_min_dimension: int = Field(900, alias="IMAGE_MIN_DIMENSION", ge=256, le=2400)
    ai_rate_limit: int = Field(30, alias="AI_RATE_LIMIT", ge=1)
    ai_daily_request_limit: int = Field(50, alias="AI_DAILY_REQUEST_LIMIT", ge=1)
    ai_monthly_request_limit: int = Field(500, alias="AI_MONTHLY_REQUEST_LIMIT", ge=1)
    ai_daily_token_limit: int = Field(100_000, alias="AI_DAILY_TOKEN_LIMIT", ge=1)
    ai_monthly_token_limit: int = Field(1_000_000, alias="AI_MONTHLY_TOKEN_LIMIT", ge=1)
    ai_daily_cost_limit: float = Field(5.0, alias="AI_DAILY_COST_LIMIT", gt=0)
    ai_monthly_cost_limit: float = Field(50.0, alias="AI_MONTHLY_COST_LIMIT", gt=0)
    ai_estimated_cost_per_token: float = Field(0.00001, alias="AI_ESTIMATED_COST_PER_TOKEN", gt=0)
    ai_cost_currency: str = Field("USD", alias="AI_COST_CURRENCY")
    bootstrap_admin_emails: str = Field("", alias="BOOTSTRAP_ADMIN_EMAILS")
    ai_max_prompt_bytes: int = Field(6000, alias="AI_MAX_PROMPT_BYTES", ge=128)
    ai_max_input_tokens: int = Field(2000, alias="AI_MAX_INPUT_TOKENS", ge=1)
    ai_max_output_tokens: int = Field(600, alias="AI_MAX_OUTPUT_TOKENS", ge=1)
    ai_max_messages_per_conversation: int = Field(20, alias="AI_MAX_MESSAGES_PER_CONVERSATION", ge=1)
    ai_max_concurrent_requests_per_user: int = Field(2, alias="AI_MAX_CONCURRENT_REQUESTS_PER_USER", ge=1)
    ai_request_timeout_seconds: int = Field(20, alias="AI_REQUEST_TIMEOUT_SECONDS", ge=1, le=120)
    ai_stream_max_duration_seconds: int = Field(30, alias="AI_STREAM_MAX_DURATION_SECONDS", ge=1, le=300)
    # The project id is public Firebase configuration and is also embedded in
    # the mobile clients. Keeping the production default here makes OAuth and
    # push diagnostics work even when an older Render service has not yet
    # synchronized the non-secret variable from render.yaml.
    firebase_project_id: str = Field("luxury-345be", alias="FIREBASE_PROJECT_ID")
    google_application_credentials: str = Field("", alias="GOOGLE_APPLICATION_CREDENTIALS")
    google_application_credentials_json: str = Field("", alias="GOOGLE_APPLICATION_CREDENTIALS_JSON")
    firebase_service_account_json: str = Field("", alias="FIREBASE_SERVICE_ACCOUNT_JSON")
    vapid_public_key: str = Field("", alias="VAPID_PUBLIC_KEY")
    vapid_private_key: str = Field("", alias="VAPID_PRIVATE_KEY")
    vapid_subject: str = Field("", alias="VAPID_SUBJECT")
    message_max_attempts: int = Field(5, alias="MESSAGE_MAX_ATTEMPTS", ge=1, le=25)
    message_retry_base_seconds: int = Field(30, alias="MESSAGE_RETRY_BASE_SECONDS", ge=1, le=3600)
    message_retry_max_seconds: int = Field(3600, alias="MESSAGE_RETRY_MAX_SECONDS", ge=1, le=86400)
    message_lock_timeout_seconds: int = Field(300, alias="MESSAGE_LOCK_TIMEOUT_SECONDS", ge=30, le=86400)
    message_batch_size: int = Field(50, alias="MESSAGE_BATCH_SIZE", ge=1, le=500)
    message_worker_poll_seconds: float = Field(
        5.0,
        alias="MESSAGE_WORKER_POLL_SECONDS",
        ge=1.0,
        le=60.0,
    )
    message_bulk_recipient_limit: int = Field(100, alias="MESSAGE_BULK_RECIPIENT_LIMIT", ge=1, le=5000)
    message_retention_days: int = Field(30, alias="MESSAGE_RETENTION_DAYS", ge=1, le=3650)
    dead_letter_retention_days: int = Field(90, alias="DEAD_LETTER_RETENTION_DAYS", ge=1, le=3650)
    new_product_days: int = Field(30, alias="NEW_PRODUCT_DAYS", ge=1, le=3650)
    cart_max_quantity_per_item: int = Field(99, alias="CART_MAX_QUANTITY_PER_ITEM", ge=1, le=10000)
    allowed_payment_methods: str = Field(
        "cash,cash_on_delivery,CASH_ON_DELIVERY,wallet_transfer,bank_transfer,transfer,"
        "HASEB_KURAIMI,JAIB,JAWALI,YEMEN_WALLET,ONE_CASH",
        alias="ALLOWED_PAYMENT_METHODS",
    )

    @field_validator("database_url", "database_migration_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        if value.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://")):
            parsed = urlsplit(value)
            scheme = "postgresql+asyncpg"
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            sslmode = query.pop("sslmode", None)
            query.pop("channel_binding", None)
            if sslmode and sslmode.lower() != "disable":
                query.setdefault("ssl", sslmode)
            return urlunsplit((scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must point to PostgreSQL using asyncpg")
        return value

    @field_validator("jwt_secret")
    @classmethod
    def reject_placeholder_secret(cls, value: str) -> str:
        if value.upper().startswith("CHANGE_ME"):
            raise ValueError("JWT_SECRET must be replaced with a random secret")
        return value

    @field_validator("app_env")
    @classmethod
    def normalize_app_env(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {"development", "recovery_qa", "test", "staging", "production"}
        if normalized not in allowed:
            raise ValueError(f"APP_ENV must be one of {sorted(allowed)}")
        return normalized

    @field_validator("backup_offsite_provider")
    @classmethod
    def normalize_backup_offsite_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"disabled", "filesystem", "s3"}:
            raise ValueError("BACKUP_OFFSITE_PROVIDER must be disabled, filesystem, or s3")
        return normalized

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def allowed_realtime_origins(self) -> list[str]:
        configured = [item.strip().rstrip("/") for item in self.realtime_allowed_origins.split(",") if item.strip()]
        defaults = (
            sorted(OFFICIAL_FRONTEND_ORIGINS)
            if self.app_env == "production"
            else [self.frontend_public_url.rstrip("/"), self.app_public_url.rstrip("/")]
        )
        seen: set[str] = set()
        result: list[str] = []
        for value in [*configured, *defaults]:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @property
    def password_reset_redirect_allowlist(self) -> list[str]:
        configured = [item.strip() for item in self.password_reset_allowed_redirects.split(",") if item.strip()]
        defaults = [
            self.app_public_url.rstrip("/"),
            f"{self.app_public_url.rstrip('/')}/reset-password",
            self.frontend_public_url.rstrip("/"),
            f"{self.frontend_public_url.rstrip('/')}/reset-password",
            self.flutter_reset_deep_link,
        ]
        seen: set[str] = set()
        result: list[str] = []
        for value in [*configured, *defaults]:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @property
    def trusted_proxy_set(self) -> set[str]:
        return {item.strip() for item in self.trusted_proxy_ips.split(",") if item.strip()}

    @property
    def ai_model_allowlist_set(self) -> set[str]:
        models = {item.strip() for item in self.ai_model_allowlist.split(",") if item.strip()}
        if self.ai_default_model.strip():
            models.add(self.ai_default_model.strip())
        return models

    @property
    def resolved_ai_api_key(self) -> str:
        return self.ai_api_key or self.gemini_api_key or self.google_api_key

    @property
    def database_name(self) -> str:
        return urlsplit(self.database_url).path.lstrip("/")

    @property
    def is_test_environment(self) -> bool:
        return self.app_env == "test" and self.database_is_test

    @property
    def database_is_test(self) -> bool:
        name = self.database_name.lower()
        return name == "luxury_test" or name.endswith("_test") or "_e2e_test" in name

    @property
    def fixtures_enabled(self) -> bool:
        return self.is_test_environment and self.allow_test_fixtures

    @property
    def payment_method_allowlist(self) -> set[str]:
        return {item.strip() for item in self.allowed_payment_methods.split(",") if item.strip()}

    @property
    def read_only_runtime(self) -> bool:
        return self.app_env == "recovery_qa"

    @property
    def storage_environment(self) -> str:
        return f"{self.app_env}:{self.resolved_upload_dir.name}"

    @model_validator(mode="after")
    def validate_environment_consistency(self) -> "Settings":
        database_name = self.database_name.lower()
        self.storage_provider = self.storage_provider.strip().lower()
        if self.storage_provider not in {"local", "r2"}:
            raise ValueError("STORAGE_PROVIDER must be either local or r2")
        if self.app_env == "production" and self.storage_provider != "r2":
            raise ValueError("STORAGE_PROVIDER=r2 is required in production; Render must not store uploaded images")
        if self.storage_provider == "r2":
            required_r2_values = {
                "R2_ENDPOINT_URL": self.r2_endpoint_url,
                "R2_BUCKET": self.r2_bucket,
                "R2_ACCESS_KEY_ID": self.r2_access_key_id,
                "R2_SECRET_ACCESS_KEY": self.r2_secret_access_key,
                "R2_PUBLIC_BASE_URL": self.r2_public_base_url,
            }
            missing_r2_values = [name for name, value in required_r2_values.items() if not str(value).strip()]
            if missing_r2_values:
                raise ValueError(f"R2 storage requires: {', '.join(missing_r2_values)}")
        if self.allow_test_fixtures and self.app_env != "test":
            raise ValueError("ALLOW_TEST_FIXTURES=true is only allowed when APP_ENV=test")
        if self.app_env == "test" and not self.database_is_test:
            raise ValueError("APP_ENV=test requires a trusted test database name")
        if self.app_env != "test" and self.database_is_test:
            raise ValueError("test database names are not allowed outside APP_ENV=test")
        if self.app_env in {"recovery_qa", "staging", "production"}:
            forbidden = ("test", "dev", "development", "local")
            if any(marker in database_name for marker in forbidden):
                raise ValueError(f"APP_ENV={self.app_env} cannot use non-operational database {self.database_name}")
        if self.app_env in {"staging", "production"} and self.backup_offsite_provider == "filesystem":
            self.backup_offsite_provider = "disabled"
        if self.backup_offsite_provider == "s3" and not self.backup_s3_bucket:
            raise ValueError("BACKUP_S3_BUCKET is required when BACKUP_OFFSITE_PROVIDER=s3")
        if self.app_env == "production":
            render_public_url = self.render_public_url.rstrip("/")
            render_parts = urlsplit(render_public_url)
            if render_parts.scheme != "https" or not render_parts.hostname or not render_parts.hostname.endswith(".onrender.com"):
                raise ValueError("RENDER_PUBLIC_URL must be an https Render service URL")
            configured_origins = self.allowed_origins
            forbidden_origins = ("localhost", "127.0.0.1", "10.0.2.2", "0.0.0.0")
            if any(origin == "*" for origin in configured_origins):
                raise ValueError("CORS_ORIGINS cannot contain '*' in production")
            if any(any(marker in origin.lower() for marker in forbidden_origins) for origin in configured_origins):
                raise ValueError("CORS_ORIGINS cannot contain localhost or emulator origins in production")
            if not configured_origins:
                raise ValueError("CORS_ORIGINS must be explicitly configured in production")
            normalized_origins = {origin.rstrip("/") for origin in configured_origins}
            if normalized_origins != OFFICIAL_FRONTEND_ORIGINS:
                extra_origins = ", ".join(sorted(normalized_origins - OFFICIAL_FRONTEND_ORIGINS))
                missing_origins = ", ".join(sorted(OFFICIAL_FRONTEND_ORIGINS - normalized_origins))
                details = "; ".join(
                    filter(
                        None,
                        [
                            f"extra: {extra_origins}" if extra_origins else "",
                            f"missing: {missing_origins}" if missing_origins else "",
                        ],
                    )
                )
                raise ValueError(f"CORS_ORIGINS must contain exactly the two production website origins ({details})")
            realtime_origins = {origin.rstrip("/") for origin in self.allowed_realtime_origins}
            if realtime_origins != OFFICIAL_FRONTEND_ORIGINS:
                raise ValueError("REALTIME_ALLOWED_ORIGINS must contain exactly the two production website origins")
            if missing_origins := OFFICIAL_FRONTEND_ORIGINS - normalized_origins:
                joined = ", ".join(sorted(missing_origins))
                raise ValueError(f"CORS_ORIGINS must include production website origins: {joined}")
            frontend_origin = self.frontend_public_url.rstrip("/")
            if frontend_origin and frontend_origin not in OFFICIAL_FRONTEND_ORIGINS:
                raise ValueError("FRONTEND_PUBLIC_URL must use the official production website origin")
            production_http_urls = {
                "API_BASE_URL": (self.api_base_url, OFFICIAL_PUBLIC_API_ORIGIN),
                "APP_PUBLIC_URL": (self.app_public_url, OFFICIAL_PUBLIC_API_ORIGIN),
            }
            for name, (value, expected) in production_http_urls.items():
                normalized = value.rstrip("/")
                if normalized != expected:
                    raise ValueError(f"{name} must use the official public API domain in production")
                if _contains_forbidden_public_marker(normalized):
                    raise ValueError(
                        f"{name} cannot point to local, legacy, Supabase, or alternate backends in production"
                    )
            ws_normalized = self.ws_base_url.rstrip("/")
            expected_ws_origin = OFFICIAL_PUBLIC_WS_ORIGIN
            if ws_normalized != expected_ws_origin:
                raise ValueError("WS_BASE_URL must use the official public API domain in production")
            if _contains_forbidden_public_marker(ws_normalized):
                raise ValueError("WS_BASE_URL cannot point to local, legacy, Supabase, or alternate backends in production")
        return self

    def require_test_fixtures_enabled(self, operation: str = "test fixtures") -> None:
        if not self.fixtures_enabled:
            raise RuntimeError(
                f"{operation} requires APP_ENV=test, a test database name, "
                "and ALLOW_TEST_FIXTURES=true"
            )

    @cached_property
    def resolved_upload_dir(self) -> Path:
        primary = self._absolute_upload_path(self.upload_dir)
        if _is_usable_upload_dir(primary):
            return primary
        fallback = self._upload_fallback_path()
        if fallback is not None and _is_usable_upload_dir(fallback):
            return fallback
        raise RuntimeError(f"UPLOAD_DIR is not writable: {primary}")

    def _upload_fallback_path(self) -> Path | None:
        if self.upload_fallback_dir is not None:
            return self._absolute_upload_path(self.upload_fallback_dir)
        if self.app_env in {"staging", "production"}:
            return (Path(tempfile.gettempdir()) / "luxury-backend-uploads").resolve()
        return None

    @cached_property
    def resolved_backup_storage_dir(self) -> Path:
        return self._safe_runtime_dir(self.backup_storage_dir)

    @cached_property
    def resolved_backup_offsite_dir(self) -> Path:
        return self._safe_runtime_dir(self.backup_offsite_dir)

    @cached_property
    def resolved_backup_encryption_key_file(self) -> Path:
        if self.backup_encryption_key_file is not None:
            return self._absolute_upload_path(self.backup_encryption_key_file)
        return (Path.home() / ".luxury-secrets" / f"{self.app_env}-backup-fernet.key").resolve()

    def _safe_runtime_dir(self, path: Path) -> Path:
        target = self._absolute_upload_path(path)
        target.mkdir(parents=True, exist_ok=True)
        if target == self.resolved_upload_dir or self.resolved_upload_dir in target.parents:
            raise RuntimeError("backup storage must not be inside public upload storage")
        if not _is_usable_upload_dir(target):
            raise RuntimeError(f"Backup directory is not writable: {target}")
        return target

    @staticmethod
    def _absolute_upload_path(path: Path) -> Path:
        if not path.is_absolute():
            path = PROJECT_DIR / path
        return path.resolve()


def _is_usable_upload_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _contains_forbidden_public_marker(value: str) -> bool:
    normalized = value.lower()
    return any(marker in normalized for marker in FORBIDDEN_PUBLIC_ENDPOINT_MARKERS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
