"""Schema shape: the constraints that enforce the hard rules must exist."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.db.base import Base
from app.models import AuditLog, Client, ConnectorName, ToolCredential, ToolMapping
from app.security.crypto import CredentialCipher


def _unique_column_sets(table: object) -> set[frozenset[str]]:
    return {
        frozenset(c.name for c in constraint.columns)
        for constraint in table.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, UniqueConstraint)
    }


def _fk(table: object, column: str) -> ForeignKeyConstraint:
    for constraint in table.constraints:  # type: ignore[attr-defined]
        if isinstance(constraint, ForeignKeyConstraint) and column in constraint.column_keys:
            return constraint
    raise AssertionError(f"no foreign key on {column}")


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "clients",
        "sites",
        "tool_mappings",
        "tool_credentials",
        "audit_log",
        "conversations",
        "conversation_messages",
        "report_configs",
        "tbr_reports",
        "collected_events",
        "collection_coverage",
        "coverage_exclusions",
        "coverage_snapshots",
        "devices",
        "device_vendor_links",
        "device_unresolved",
        "device_agreements",
        "ticket_workspaces",
        "ticket_plans",
        "ticket_outcomes",
        "shadow_runs",
        "shadow_predictions",
        "shadow_enrichments",
        "shadow_batches",
        "shadow_samples",
        "shadow_sample_tickets",
        "labelling_sets",
        "labelling_items",
        "labelling_answers",
        "ticket_suggestions",
        "suggestion_reviews",
        "blind_checks",
        "automation_runs",
        "automation_steps",
        "automation_switches",
        "automation_kill_switches",
        "automation_trigger_switches",
        "automation_trigger_log",
        "automation_notes",
        "automation_note_modes",
        "automation_alert_tickets",
        "automation_reports",
        "automation_practice_records",
        "organisations",
        "users",
        "invites",
        "sessions",
    }


def test_a_run_is_a_record_and_nothing_may_take_it_away() -> None:
    """A run that changed something at a client is the record of that change,
    so its keys to the client and to the people who started and approved it
    are RESTRICT — the rule every judgement table here follows."""
    from app.models import AutomationRun

    table = AutomationRun.__table__
    assert _fk(table, "client_id").ondelete == "RESTRICT"
    assert _fk(table, "started_by_user_id").ondelete == "RESTRICT"
    assert _fk(table, "approved_by_user_id").ondelete == "RESTRICT"
    # And one run at a time per client is the database's rule, not a worker's
    # memory: a partial unique index over the running ones.
    running = next(
        index for index in table.indexes if index.name.endswith("one_running_per_client")
    )
    assert running.unique
    assert "state = 'running'" in str(running.dialect_options["postgresql"]["where"])


def test_nothing_done_to_a_thread_can_remove_an_audit_row() -> None:
    """`audit_log.conversation_id` is RESTRICT: threads are archived, never cascaded."""
    assert _fk(AuditLog.__table__, "conversation_id").ondelete == "RESTRICT"
    assert AuditLog.__table__.c.conversation_id.nullable is True


def test_credentials_are_unique_per_client_and_connector() -> None:
    """Credential resolution is keyed by (client_id, connector_name)."""
    assert frozenset({"client_id", "connector_name"}) in _unique_column_sets(
        ToolCredential.__table__
    )


def test_a_vendor_tenant_belongs_to_exactly_one_site() -> None:
    """The database half of hard rule 1: no two clients share an external org."""
    uniques = _unique_column_sets(ToolMapping.__table__)
    # One site per vendor organisation, across every client...
    assert frozenset({"connector_name", "external_id"}) in uniques
    # ...and a site may hold several organisations on one connector (migration
    # 0013: a customer with two GravityZone companies), so that pair is not unique.
    assert frozenset({"site_id", "connector_name"}) not in uniques


def test_a_tenant_level_mapping_never_carries_a_vendor_id() -> None:
    checks = [
        str(constraint.sqltext)
        for constraint in ToolMapping.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert "site_id IS NOT NULL OR external_id IS NULL" in checks
    assert ToolMapping.__table__.c.external_id.nullable is True


def test_one_tenant_level_mapping_per_client_and_connector() -> None:
    """Partial, because the site-level rows deliberately repeat that pair."""
    indexes = {index.name: index for index in ToolMapping.__table__.indexes}
    tenant_level = indexes["uq_tool_mappings_tenant_level"]

    assert tenant_level.unique is True
    assert [column.name for column in tenant_level.columns] == ["client_id", "connector_name"]
    assert "site_id IS NULL" in str(tenant_level.dialect_options["postgresql"]["where"])


def test_sites_belong_to_one_client_and_their_slugs_are_client_scoped() -> None:
    from app.models import Site

    uniques = _unique_column_sets(Site.__table__)
    assert frozenset({"client_id", "slug"}) in uniques
    # Deliberately not globally unique: two franchisees may both have an
    # "acme" customer, and neither should block the other.
    assert frozenset({"slug"}) not in uniques
    assert _fk(Site.__table__, "client_id").ondelete == "CASCADE"


def test_client_slug_has_a_format_constraint() -> None:
    checks = [c for c in Client.__table__.constraints if isinstance(c, CheckConstraint)]
    assert any("slug" in str(c.sqltext) for c in checks)


def test_credentials_and_mappings_cascade_but_audit_does_not() -> None:
    assert _fk(ToolCredential.__table__, "client_id").ondelete == "CASCADE"
    assert _fk(ToolMapping.__table__, "client_id").ondelete == "CASCADE"
    # The audit trail must survive; deleting a referenced client is blocked.
    assert _fk(AuditLog.__table__, "client_id").ondelete == "RESTRICT"


def test_audit_log_records_who_what_when_and_outcome() -> None:
    """Hard rule 4."""
    columns = set(AuditLog.__table__.columns.keys())
    assert {
        "actor_id",
        "client_id",
        "created_at",
        "event_type",
        "outcome",
        "request_id",
        "tool_name",
        "payload",
    } <= columns


def test_audit_client_id_is_nullable_so_rejected_requests_are_still_logged() -> None:
    assert AuditLog.__table__.c.client_id.nullable is True
    assert AuditLog.__table__.c.client_ref.nullable is True
    # But identity and outcome are never optional.
    assert AuditLog.__table__.c.actor_id.nullable is False
    assert AuditLog.__table__.c.outcome.nullable is False


def test_audit_log_is_indexed_for_the_queries_we_will_run() -> None:
    index_columns = {tuple(c.name for c in index.columns) for index in AuditLog.__table__.indexes}
    assert ("request_id", "id") in index_columns
    assert ("client_id", "created_at") in index_columns


@pytest.mark.parametrize("table_name", ["clients", "tool_mappings", "tool_credentials"])
def test_timestamps_are_timezone_aware(table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    for column_name in ("created_at", "updated_at"):
        assert table.c[column_name].type.timezone is True


def test_credential_repr_leaks_nothing(cipher: CredentialCipher) -> None:
    credential = ToolCredential(
        client_id=uuid.uuid4(),
        connector_name=ConnectorName.NINJAONE,
    )
    credential.set_secrets({"client_secret": "s3cr3t"}, cipher=cipher)

    rendered = repr(credential)
    assert "s3cr3t" not in rendered
    assert "secret_ciphertext" not in rendered
    assert "ninjaone" in rendered


def test_credential_secret_roundtrip(cipher: CredentialCipher) -> None:
    credential = ToolCredential(client_id=uuid.uuid4(), connector_name=ConnectorName.AUTOTASK)
    credential.set_secrets({"api_key": "k", "tracker": "t"}, cipher=cipher)

    assert credential.encryption_key_id == cipher.active_key_id
    assert credential.get_secrets(cipher=cipher) == {"api_key": "k", "tracker": "t"}


def test_credential_expiry() -> None:
    import datetime as dt

    credential = ToolCredential(client_id=uuid.uuid4(), connector_name=ConnectorName.COVE)
    assert credential.is_expired() is False

    credential.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    assert credential.is_expired() is True


def test_connector_names_cover_the_phase_one_stack() -> None:
    assert {c.value for c in ConnectorName} >= {
        "ninjaone",
        "autotask",
        "bitdefender",
        "cove",
        "connectsecure",
        "blackpoint",
    }
