"""Tests for M4 PR 4.2: Matcher Parser."""
from __future__ import annotations

import pytest

from packages.discovery.matcher_parser import (
    InvalidMatcherError,
    Matcher,
    parse_matchers,
    to_alertmanager_filter,
)


class TestParseMatchers:
    def test_parse_equal(self):
        result = parse_matchers(["severity=critical"])
        assert len(result) == 1
        assert result[0].label == "severity"
        assert result[0].operator == "="
        assert result[0].value == "critical"

    def test_parse_not_equal(self):
        result = parse_matchers(["namespace!=default"])
        assert result[0].operator == "!="
        assert result[0].value == "default"

    def test_parse_regex(self):
        result = parse_matchers(['namespace=~"prod-.*"'])
        assert result[0].operator == "=~"
        assert result[0].value == "prod-.*"

    def test_parse_not_regex(self):
        result = parse_matchers(['team!~"test-.*"'])
        assert result[0].operator == "!~"
        assert result[0].value == "test-.*"

    def test_quoted_comma_not_split(self):
        result = parse_matchers(['team="sre,ops"'])
        assert result[0].value == "sre,ops"

    def test_single_quoted_value(self):
        result = parse_matchers(["team='sre'"])
        assert result[0].value == "sre"

    def test_multiple_matchers(self):
        result = parse_matchers(["severity=critical", 'namespace=~"prod"'])
        assert len(result) == 2

    def test_empty_string_skipped(self):
        result = parse_matchers(["", "severity=critical", "  "])
        assert len(result) == 1

    def test_invalid_label_name(self):
        with pytest.raises(InvalidMatcherError):
            parse_matchers(["123bad=value"])

    def test_invalid_regex(self):
        with pytest.raises(InvalidMatcherError):
            parse_matchers(['name=~"[invalid"'])

    def test_no_operator(self):
        with pytest.raises(InvalidMatcherError):
            parse_matchers(["novalidoperatorhere"])


class TestToAlertmanagerFilter:
    def test_single_matcher(self):
        matchers = [Matcher(label="severity", operator="=", value="critical")]
        result = to_alertmanager_filter(matchers)
        assert result == ["severity=critical"]

    def test_multiple_matchers(self):
        matchers = [
            Matcher(label="severity", operator="=", value="critical"),
            Matcher(label="namespace", operator="=~", value="prod-.*"),
        ]
        result = to_alertmanager_filter(matchers)
        assert len(result) == 2
        assert result[0] == "severity=critical"
        assert result[1] == "namespace=~prod-.*"
