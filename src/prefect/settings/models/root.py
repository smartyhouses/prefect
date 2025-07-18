import warnings
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Iterable,
    Mapping,
    Optional,
)
from urllib.parse import urlparse

from pydantic import BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict
from typing_extensions import Self

from prefect.settings.base import PrefectBaseSettings, build_settings_config
from prefect.settings.models.tasks import TasksSettings
from prefect.settings.models.testing import TestingSettings
from prefect.settings.models.worker import WorkerSettings
from prefect.utilities.collections import deep_merge_dicts, set_in_dict

from ._defaults import (
    default_database_connection_url,
    default_profiles_path,
    default_ui_url,
    substitute_home_template,
)
from .api import APISettings
from .cli import CLISettings
from .client import ClientSettings
from .cloud import CloudSettings
from .deployments import DeploymentsSettings
from .experiments import ExperimentsSettings
from .flows import FlowsSettings
from .internal import InternalSettings
from .logging import LoggingSettings
from .results import ResultsSettings
from .runner import RunnerSettings
from .server import ServerSettings

if TYPE_CHECKING:
    from prefect.settings.legacy import Setting


class Settings(PrefectBaseSettings):
    """
    Settings for Prefect using Pydantic settings.

    See https://docs.pydantic.dev/latest/concepts/pydantic_settings
    """

    model_config: ClassVar[SettingsConfigDict] = build_settings_config()

    home: Annotated[Path, BeforeValidator(lambda x: Path(x).expanduser())] = Field(
        default=Path("~") / ".prefect",
        description="The path to the Prefect home directory. Defaults to ~/.prefect",
    )

    profiles_path: Annotated[Path, BeforeValidator(substitute_home_template)] = Field(
        default_factory=default_profiles_path,
        description=(
            "The path to a profiles configuration file. Supports $PREFECT_HOME templating."
            " Defaults to $PREFECT_HOME/profiles.toml."
        ),
    )

    debug_mode: bool = Field(
        default=False,
        description="If True, enables debug mode which may provide additional logging and debugging features.",
    )

    api: APISettings = Field(
        default_factory=APISettings,
        description="Settings for interacting with the Prefect API",
    )

    cli: CLISettings = Field(
        default_factory=CLISettings,
        description="Settings for controlling CLI behavior",
    )

    client: ClientSettings = Field(
        default_factory=ClientSettings,
        description="Settings for controlling API client behavior",
    )

    cloud: CloudSettings = Field(
        default_factory=CloudSettings,
        description="Settings for interacting with Prefect Cloud",
    )

    deployments: DeploymentsSettings = Field(
        default_factory=DeploymentsSettings,
        description="Settings for configuring deployments defaults",
    )

    experiments: ExperimentsSettings = Field(
        default_factory=ExperimentsSettings,
        description="Settings for controlling experimental features",
    )

    flows: FlowsSettings = Field(
        default_factory=FlowsSettings,
        description="Settings for controlling flow behavior",
    )

    internal: InternalSettings = Field(
        default_factory=InternalSettings,
        description="Settings for internal Prefect machinery",
    )

    logging: LoggingSettings = Field(
        default_factory=LoggingSettings,
        description="Settings for controlling logging behavior",
    )

    results: ResultsSettings = Field(
        default_factory=ResultsSettings,
        description="Settings for controlling result storage behavior",
    )

    runner: RunnerSettings = Field(
        default_factory=RunnerSettings,
        description="Settings for controlling runner behavior",
    )

    server: ServerSettings = Field(
        default_factory=ServerSettings,
        description="Settings for controlling server behavior",
    )

    tasks: TasksSettings = Field(
        default_factory=TasksSettings,
        description="Settings for controlling task behavior",
    )

    testing: TestingSettings = Field(
        default_factory=TestingSettings,
        description="Settings used during testing",
    )

    worker: WorkerSettings = Field(
        default_factory=WorkerSettings,
        description="Settings for controlling worker behavior",
    )

    ui_url: Optional[str] = Field(
        default=None,
        description="The URL of the Prefect UI. If not set, the client will attempt to infer it.",
    )

    silence_api_url_misconfiguration: bool = Field(
        default=False,
        description="""
        If `True`, disable the warning when a user accidentally misconfigure its `PREFECT_API_URL`
        Sometimes when a user manually set `PREFECT_API_URL` to a custom url,reverse-proxy for example,
        we would like to silence this warning so we will set it to `FALSE`.
        """,
    )

    ###########################################################################
    # allow deprecated access to PREFECT_SOME_SETTING_NAME

    def __getattribute__(self, name: str) -> Any:
        from prefect.settings.legacy import _env_var_to_accessor

        if name.startswith("PREFECT_"):
            accessor = _env_var_to_accessor(name)
            warnings.warn(
                f"Accessing `Settings().{name}` is deprecated. Use `Settings().{accessor}` instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            path = accessor.split(".")
            value = super().__getattribute__(path[0])
            for key in path[1:]:
                value = getattr(value, key)
            return value
        return super().__getattribute__(name)

    ###########################################################################

    @model_validator(mode="after")
    def post_hoc_settings(self) -> Self:
        """Handle remaining complex default assignments that aren't yet migrated to dependent settings.

        With Pydantic 2.10's dependent settings feature, we've migrated simple path-based defaults
        to use default_factory. The remaining items here require access to the full Settings instance
        or have complex interdependencies that will be migrated in future PRs.
        """
        if self.ui_url is None:
            self.ui_url = default_ui_url(self)
            self.__pydantic_fields_set__.remove("ui_url")
        if self.server.ui.api_url is None:
            if self.api.url:
                self.server.ui.api_url = self.api.url
                self.server.ui.__pydantic_fields_set__.remove("api_url")
            else:
                self.server.ui.api_url = (
                    f"http://{self.server.api.host}:{self.server.api.port}/api"
                )
                self.server.ui.__pydantic_fields_set__.remove("api_url")
        if self.debug_mode or self.testing.test_mode:
            self.logging.level = "DEBUG"
            self.internal.logging_level = "DEBUG"
            self.logging.__pydantic_fields_set__.remove("level")
            self.internal.__pydantic_fields_set__.remove("logging_level")

        # Set default database connection URL if not provided
        if self.server.database.connection_url is None:
            self.server.database.connection_url = default_database_connection_url(self)
            self.server.database.__pydantic_fields_set__.remove("connection_url")
        db_url = self.server.database.connection_url.get_secret_value()
        if (
            "PREFECT_API_DATABASE_PASSWORD" in db_url
            or "PREFECT_SERVER_DATABASE_PASSWORD" in db_url
        ):
            if self.server.database.password is None:
                raise ValueError(
                    "database password is None - please set PREFECT_SERVER_DATABASE_PASSWORD"
                )
            db_url = db_url.replace(
                "${PREFECT_API_DATABASE_PASSWORD}",
                self.server.database.password.get_secret_value()
                if self.server.database.password
                else "",
            )
            db_url = db_url.replace(
                "${PREFECT_SERVER_DATABASE_PASSWORD}",
                self.server.database.password.get_secret_value()
                if self.server.database.password
                else "",
            )
            self.server.database.connection_url = SecretStr(db_url)
            self.server.database.__pydantic_fields_set__.remove("connection_url")

        return self

    @model_validator(mode="after")
    def emit_warnings(self) -> Self:
        """More post-hoc validation of settings, including warnings for misconfigurations."""
        if not self.silence_api_url_misconfiguration:
            _warn_on_misconfigured_api_url(self)
        return self

    ##########################################################################
    # Settings methods

    def copy_with_update(
        self: Self,
        updates: Optional[Mapping["Setting", Any]] = None,
        set_defaults: Optional[Mapping["Setting", Any]] = None,
        restore_defaults: Optional[Iterable["Setting"]] = None,
    ) -> Self:
        """
        Create a new Settings object with validation.

        Arguments:
            updates: A mapping of settings to new values. Existing values for the
                given settings will be overridden.
            set_defaults: A mapping of settings to new default values. Existing values for
                the given settings will only be overridden if they were not set.
            restore_defaults: An iterable of settings to restore to their default values.

        Returns:
            A new Settings object.
        """
        # To restore defaults, we need to resolve the setting path and then
        # set the default value on the new settings object. When restoring
        # defaults, all settings sources will be ignored.
        restore_defaults_obj: dict[str, Any] = {}
        for r in restore_defaults or []:
            path = r.accessor.split(".")
            model = self
            model_cls = model.__class__
            model_fields = model_cls.model_fields
            for key in path[:-1]:
                model_field = model_fields[key]
                model_cls = model_field.annotation
                if model_cls is None:
                    raise ValueError(f"Invalid setting path: {r.accessor}")
                model_fields = model_cls.model_fields

            model_field = model_fields[path[-1]]
            assert model_field is not None, f"Invalid setting path: {r.accessor}"
            if hasattr(model_field, "default"):
                default = model_field.default
            elif (
                hasattr(model_field, "default_factory") and model_field.default_factory
            ):
                default = model_field.default_factory()
            else:
                raise ValueError(f"No default value for setting: {r.accessor}")
            set_in_dict(
                restore_defaults_obj,
                r.accessor,
                default,
            )
        updates = updates or {}
        set_defaults = set_defaults or {}

        set_defaults_obj: dict[str, Any] = {}
        for setting, value in set_defaults.items():
            set_in_dict(set_defaults_obj, setting.accessor, value)

        updates_obj: dict[str, Any] = {}
        for setting, value in updates.items():
            set_in_dict(updates_obj, setting.accessor, value)

        new_settings = self.__class__.model_validate(
            deep_merge_dicts(
                set_defaults_obj,
                self.model_dump(exclude_unset=True),
                restore_defaults_obj,
                updates_obj,
            )
        )
        return new_settings

    def hash_key(self) -> str:
        """
        Return a hash key for the settings object.  This is needed since some
        settings may be unhashable, like lists.
        """
        env_variables = self.to_environment_variables()
        return str(hash(tuple((key, value) for key, value in env_variables.items())))


def _warn_on_misconfigured_api_url(settings: "Settings"):
    """
    Validator for settings warning if the API URL is misconfigured.
    """
    api_url = settings.api.url
    if api_url is not None:
        misconfigured_mappings = {
            "app.prefect.cloud": (
                "`PREFECT_API_URL` points to `app.prefect.cloud`. Did you"
                " mean `api.prefect.cloud`?"
            ),
            "account/": (
                "`PREFECT_API_URL` uses `/account/` but should use `/accounts/`."
            ),
            "workspace/": (
                "`PREFECT_API_URL` uses `/workspace/` but should use `/workspaces/`."
            ),
        }
        warnings_list: list[str] = []

        for misconfig, warning in misconfigured_mappings.items():
            if misconfig in api_url:
                warnings_list.append(warning)

        parsed_url = urlparse(api_url)
        if (
            parsed_url.path
            and "api.prefect.cloud" in api_url
            and not parsed_url.path.startswith("/api")
        ):
            warnings_list.append(
                "`PREFECT_API_URL` should have `/api` after the base URL."
            )

        if warnings_list:
            example = 'e.g. PREFECT_API_URL="https://api.prefect.cloud/api/accounts/[ACCOUNT-ID]/workspaces/[WORKSPACE-ID]"'
            warnings_list.append(example)

            warnings.warn("\n".join(warnings_list), stacklevel=2)

    return settings


def canonical_environment_prefix(settings: "Settings") -> str:
    return settings.model_config.get("env_prefix") or ""
