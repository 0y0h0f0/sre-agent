"""Tests for M4 PR 4.3: Scope Validation + PR 4.4: Allowlist mapping."""
from __future__ import annotations

from packages.discovery.matcher_parser import (
    AlertPollFilters,
    _allowlist_to_server_matchers,
    can_map_to_server_side,
    has_valid_scope,
)


class TestHasValidScope:
    def test_receiver_valid(self):
        assert has_valid_scope(AlertPollFilters(receiver="team-x"))

    def test_namespace_allowlist_valid(self):
        assert has_valid_scope(AlertPollFilters(namespace_allowlist=["prod"]))

    def test_service_allowlist_valid(self):
        assert has_valid_scope(AlertPollFilters(service_allowlist=["checkout"]))

    def test_non_severity_matcher_valid(self):
        assert has_valid_scope(AlertPollFilters(extra_matchers=["team=sre"]))

    def test_cluster_matcher_valid(self):
        assert has_valid_scope(AlertPollFilters(extra_matchers=["cluster=prod-1"]))

    def test_severity_only_not_valid(self):
        assert not has_valid_scope(AlertPollFilters(extra_matchers=["severity=critical"]))

    def test_priority_only_not_valid(self):
        assert not has_valid_scope(AlertPollFilters(extra_matchers=["priority=P1"]))

    def test_severity_and_priority_not_valid(self):
        assert not has_valid_scope(
            AlertPollFilters(extra_matchers=["severity=critical", "priority=P1"])
        )

    def test_mixed_severity_and_namespace_valid(self):
        assert has_valid_scope(
            AlertPollFilters(
                extra_matchers=["severity=critical"],
                namespace_allowlist=["prod"],
            )
        )

    def test_empty_scope_disabled(self):
        assert not has_valid_scope(AlertPollFilters())

    def test_team_matcher_valid(self):
        assert has_valid_scope(AlertPollFilters(extra_matchers=["team=sre"]))


class TestAllowlistToServerMatchers:
    def test_namespace_allowlist_to_regex(self):
        result = _allowlist_to_server_matchers(
            namespace_allowlist=["prod", "staging"],
            service_allowlist=[],
        )
        assert len(result) == 1
        assert 'namespace=~"' in result[0]
        assert "prod" in result[0]
        assert "staging" in result[0]

    def test_service_allowlist_to_regex(self):
        result = _allowlist_to_server_matchers(
            namespace_allowlist=[],
            service_allowlist=["checkout", "payments"],
            service_label="app",
        )
        assert len(result) == 1
        assert 'app=~"' in result[0]
        assert "checkout" in result[0]

    def test_both_allowlists(self):
        result = _allowlist_to_server_matchers(
            namespace_allowlist=["prod"],
            service_allowlist=["svc1"],
        )
        assert len(result) == 2


class TestCanMapToServerSide:
    def test_receiver_only_is_mappable(self):
        assert can_map_to_server_side(AlertPollFilters(receiver="x"))

    def test_namespace_allowlist_is_mappable(self):
        assert can_map_to_server_side(AlertPollFilters(namespace_allowlist=["ns"]))

    def test_service_allowlist_is_mappable(self):
        assert can_map_to_server_side(AlertPollFilters(service_allowlist=["svc"]))

    def test_extra_matchers_are_mappable(self):
        assert can_map_to_server_side(AlertPollFilters(extra_matchers=["team=sre"]))

    def test_empty_filters_not_mappable(self):
        assert not can_map_to_server_side(AlertPollFilters())
