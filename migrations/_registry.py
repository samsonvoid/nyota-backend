from .base import Migration
from .v001_initial_schema import InitialSchema
from .v002_rls_policies import RLSPolicies
from .v003_auto_cleanup import AutoCleanup


def get_migrations() -> list[Migration]:
    """Returns all migrations in execution order"""
    return [
        InitialSchema(),
        RLSPolicies(),
        AutoCleanup(),
    ]
