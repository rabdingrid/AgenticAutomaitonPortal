"""
validators — Pre-merge validation (GitSpace, YAML, DB/Liquibase, build checks).

Primary API: validators.validation.run_validation()
"""

from validators.validation import run_validation

__all__ = ["run_validation"]
