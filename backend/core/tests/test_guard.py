import pytest

from captain_search.connectors.guard import WriteRefused, check_read_only

OK = [
    "select 1",
    "with t as (select * from p) select count(*) from t",
    "select a from x union all select a from y",
    "SELECT q, count(*) FROM search_log WHERE results = 0 GROUP BY 1 ORDER BY 2 DESC LIMIT 100",
]
BAD = [
    "insert into t values (1)",
    "update t set a = 1",
    "delete from t",
    "drop table t",
    "create table t as select 1",
    "select 1; drop table t",
    "with d as (delete from t returning *) select * from d",
    "select * into newt from t",
    "grant all on t to bob",
    "this is not sql at all ((",
]


@pytest.mark.parametrize("sql", OK)
def test_reads_pass(sql):
    check_read_only(sql, "postgres")


@pytest.mark.parametrize("sql", BAD)
def test_writes_refused(sql):
    with pytest.raises(WriteRefused):
        check_read_only(sql, "postgres")
