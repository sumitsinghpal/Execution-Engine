"""Tests for src/execution/watchlists.py — named lists of tracked symbols."""

from src.execution.watchlists import WatchlistService


class TestAddAndListSymbols:
    def test_adding_a_symbol_creates_the_list_implicitly(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)

        service.add_symbol("Tech", "qqq")

        assert "Tech" in service.list_names()
        assert [i.symbol for i in service.items_in("Tech")] == ["QQQ"]  # uppercased

    def test_adding_the_same_symbol_twice_does_not_duplicate(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)

        service.add_symbol("Dupe-Test", "QQQ")
        service.add_symbol("Dupe-Test", "QQQ")

        assert len(service.items_in("Dupe-Test")) == 1

    def test_adding_empty_symbol_raises(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)
        import pytest
        with pytest.raises(ValueError):
            service.add_symbol("Empty-Test", "   ")

    def test_all_lists_groups_by_list_name(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)

        service.add_symbol("Group-A", "QQQ")
        service.add_symbol("Group-A", "SPY")
        service.add_symbol("Group-B", "IWM")

        lists = service.all_lists()
        assert lists["Group-A"] == ["QQQ", "SPY"]
        assert lists["Group-B"] == ["IWM"]


class TestRemoveSymbol:
    def test_removing_a_symbol_returns_true(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)
        service.add_symbol("Remove-Test", "QQQ")

        removed = service.remove_symbol("Remove-Test", "QQQ")

        assert removed is True
        assert service.items_in("Remove-Test") == []

    def test_removing_a_symbol_not_on_the_list_returns_false(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)

        assert service.remove_symbol("Never-Existed", "QQQ") is False

    def test_removing_the_last_symbol_makes_the_list_disappear(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)
        service.add_symbol("Vanish-Test", "QQQ")

        service.remove_symbol("Vanish-Test", "QQQ")

        assert "Vanish-Test" not in service.list_names()


class TestDeleteList:
    def test_delete_list_removes_every_symbol_and_returns_the_count(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)
        service.add_symbol("Delete-Test", "QQQ")
        service.add_symbol("Delete-Test", "SPY")

        removed_count = service.delete_list("Delete-Test")

        assert removed_count == 2
        assert "Delete-Test" not in service.list_names()

    def test_deleting_a_nonexistent_list_returns_zero(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = WatchlistService(session)

        assert service.delete_list("Never-Existed") == 0
