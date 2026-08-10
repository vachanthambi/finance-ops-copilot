"""
Tests for the agent layer's deterministic parts.

These deliberately make no API calls, so they run free and fast in CI. What is
worth testing here is the guard rail and the tool contract: that a destructive
query is rejected, and that every tool returns the shape the agent was promised.
"""

import pytest

from agents.base import Trace, json_safe
from agents.tools import (apply_row_limit, describe_schema, execute_sql,
                          reconcile, validate_sql, variance, worst_variances)


class TestSqlGuard:

    @pytest.mark.parametrize("sql", [
        "SELECT 1",
        "select count(*) from erp.gl_entries",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "  SELECT 1;  ",
    ])
    def test_reads_allowed(self, sql):
        assert validate_sql(sql)

    @pytest.mark.parametrize("sql", [
        "DROP TABLE erp.gl_entries",
        "DELETE FROM crm.accounts",
        "UPDATE erp.budget SET budget_amount_usd = 0",
        "INSERT INTO crm.accounts VALUES ('x')",
        "TRUNCATE erp.gl_entries",
        "GRANT ALL ON SCHEMA erp TO agent_ro",
        "SELECT 1; DROP TABLE erp.budget",
        "",
        "   ",
    ])
    def test_writes_rejected(self, sql):
        with pytest.raises(ValueError):
            validate_sql(sql)

    def test_comment_hidden_write_rejected(self):
        with pytest.raises(ValueError):
            validate_sql("SELECT 1 -- harmless\n; DROP TABLE erp.budget")

    def test_row_limit_added(self):
        assert "LIMIT" in apply_row_limit("SELECT * FROM erp.gl_entries")

    def test_existing_limit_respected(self):
        sql = "SELECT * FROM erp.gl_entries LIMIT 5"
        assert apply_row_limit(sql).count("LIMIT") == 1


class TestToolHandlers:

    def test_describe_schema(self):
        out = describe_schema()
        assert "erp.gl_entries" in out["tables"]
        assert any("amount_usd" in c for c in out["tables"]["erp.gl_entries"])

    def test_describe_single_schema(self):
        out = describe_schema(schema="billing")
        assert all(k.startswith("billing.") for k in out["tables"])

    def test_execute_sql_records_trace(self):
        trace = Trace()
        out = execute_sql("SELECT COUNT(*) AS n FROM erp.gl_entries", trace=trace)
        assert out["row_count"] == 1
        assert out["rows"][0]["n"] > 0
        assert len(trace.sql_statements()) == 1

    def test_execute_sql_blocks_write(self):
        with pytest.raises(ValueError):
            execute_sql("DELETE FROM crm.accounts")

    def test_reconcile_shape(self):
        out = reconcile()
        assert out["transactions_reconciled"] > 0
        assert not out["summary"].empty
        assert "break_type" in out["summary"].columns

    def test_reconcile_filter(self):
        out = reconcile(break_type="timing")
        assert set(out["summary"]["break_type"]) == {"timing"}

    def test_variance_bridge_reconciles(self):
        b = variance()["bridge"]
        walked = b["budget"] + b["volume"] + b["mix"] + b["price"] + b["fx"]
        assert abs(walked - b["actual"]) < 1.0

    def test_worst_variances(self):
        out = worst_variances(n=3)
        assert len(out["periods"]) == 3


class TestSerialisation:

    def test_dataframe_becomes_records(self):
        out = json_safe(reconcile()["summary"])
        assert isinstance(out, list)
        assert isinstance(out[0], dict)

    def test_nested_structures(self):
        out = json_safe({"a": [1, 2], "b": {"c": "d"}})
        assert out == {"a": [1, 2], "b": {"c": "d"}}


class TestTrace:

    def test_records_steps_in_order(self):
        t = Trace()
        t.add("orchestrator", "note", "first")
        t.add("variance", "tool_call", "second")
        assert t.agents_used() == ["orchestrator", "variance"]
        assert len(t.to_list()) == 2

    def test_render_is_readable(self):
        t = Trace()
        t.add("variance", "sql", "12 rows", detail="SELECT 1", sql="SELECT 1")
        rendered = t.render()
        assert "variance" in rendered and "SELECT 1" in rendered
