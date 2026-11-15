import discord

from steamdeals.db.database import Database
from steamdeals.db.repositories import Repository
from steamdeals.models import DealSnapshot, NewNotification
from steamdeals.notify.dispatcher import ChannelUnavailable, NotificationDispatcher


def notification(appid, final=1000, users=(1,)):
    snap = DealSnapshot(appid, f"Game {appid}", final, 2000, 50, 1800000000, True)
    return NewNotification(snap, {"specials"}, set(users))


class Sender:
    def __init__(self, repo=None):
        self.sent: list[int] = []
        self.fail: dict[int, Exception] = {}
        self.repo = repo
        self.statuses_during_send: list[str] = []

    async def __call__(self, payload):
        appid = payload["appid"]
        if self.repo is not None:
            [row] = [r for r in await self.repo.pending_notifications(100) if r["appid"] == appid]
            self.statuses_during_send.append(row["status"])
        if appid in self.fail:
            raise self.fail[appid]
        self.sent.append(appid)
        return 9000 + appid


async def fill(repo, appids):
    for a in appids:
        assert await repo.enqueue_notification(notification(a))


async def test_limit_per_cycle_oldest_first_and_remaining_stay_pending(repo):
    await fill(repo, range(1, 6))
    sender = Sender()
    d = NotificationDispatcher(repo, sender, max_per_cycle=3)
    result = await d.dispatch()
    assert sender.sent == [1, 2, 3] and result.sent == 3 and result.remaining == 2
    assert [n["appid"] for n in await repo.pending_notifications(10)] == [4, 5]
    result = await d.dispatch()
    assert sender.sent == [1, 2, 3, 4, 5] and result.remaining == 0


async def test_default_limit_is_twenty_and_nothing_is_discarded(repo):
    await fill(repo, range(1, 46))
    sender = Sender()
    d = NotificationDispatcher(repo, sender)
    await d.dispatch()
    assert len(sender.sent) == 20
    assert await repo.count_notifications("pending") == 25
    await d.dispatch()
    await d.dispatch()
    assert sender.sent == list(range(1, 46))  # strict FIFO, every one delivered exactly once
    assert await repo.count_notifications("sent") == 45


async def test_marked_sent_only_after_send_succeeds(repo):
    await fill(repo, [1])
    sender = Sender(repo)
    await NotificationDispatcher(repo, sender).dispatch()
    assert sender.statuses_during_send == ["pending"]  # still pending while send() ran
    n = await repo.get_notification(1)
    assert n["status"] == "sent" and n["discord_message_id"] == "9001" and n["sent_at"]


async def test_send_failure_keeps_pending_and_does_not_block_others(repo):
    await fill(repo, [1, 2, 3])
    sender = Sender()
    sender.fail[2] = RuntimeError("temporary glitch")
    result = await NotificationDispatcher(repo, sender).dispatch()
    assert sender.sent == [1, 3] and result.failed == 1 and not result.aborted
    n = await repo.get_notification(2)
    assert n["status"] == "pending" and n["attempts"] == 1 and "temporary glitch" in n["last_error"]
    sender.fail.clear()
    await NotificationDispatcher(repo, sender).dispatch()
    assert (await repo.get_notification(2))["status"] == "sent"
    assert (await repo.get_notification(2))["attempts"] == 2


def http_exc(cls, status):
    class Resp:
        pass

    r = Resp()
    r.status, r.reason = status, "x"
    return cls(r, "msg")


async def test_discord_errors_abort_batch_but_keep_everything_pending(repo):
    for exc in (http_exc(discord.Forbidden, 403), http_exc(discord.NotFound, 404),
                http_exc(discord.HTTPException, 429), ChannelUnavailable("gone")):
        await fill(repo, [10, 11])
        sender = Sender()
        sender.fail[10] = exc
        result = await NotificationDispatcher(repo, sender).dispatch()
        assert result.aborted and sender.sent == []  # did not hammer with the second message
        assert await repo.count_notifications("pending") == 2
        async with repo.db.transaction() as c:
            await c.execute("DELETE FROM notifications")


async def test_server_error_does_not_abort_batch(repo):
    await fill(repo, [1, 2])
    sender = Sender()
    sender.fail[1] = http_exc(discord.HTTPException, 500)
    result = await NotificationDispatcher(repo, sender).dispatch()
    assert sender.sent == [2] and not result.aborted


async def test_failure_to_mark_sent_never_loses_notification(repo, monkeypatch):
    await fill(repo, [1])
    sender = Sender()
    real = repo.mark_sent

    async def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "mark_sent", broken)
    result = await NotificationDispatcher(repo, sender).dispatch()
    assert sender.sent == [1] and result.sent == 0
    assert await repo.count_notifications("pending") == 1
    monkeypatch.setattr(repo, "mark_sent", real)
    await NotificationDispatcher(repo, sender).dispatch()
    assert sender.sent == [1, 1]  # documented at-least-once duplicate, never a loss
    assert await repo.count_notifications("sent") == 1


async def test_pending_survives_restart(tmp_path):
    path = str(tmp_path / "q.db")
    db1 = await Database(path).connect()
    r1 = Repository(db1)
    await fill(r1, [1, 2, 3])
    sender = Sender()
    await NotificationDispatcher(r1, sender, max_per_cycle=1).dispatch()
    await db1.close()

    db2 = await Database(path).connect()
    r2 = Repository(db2)
    assert [n["appid"] for n in await r2.pending_notifications(10)] == [2, 3]
    await NotificationDispatcher(r2, sender).dispatch()
    assert sender.sent == [1, 2, 3]
    await db2.close()


async def test_superseded_notification_is_not_sent_and_is_recorded(repo):
    await repo.enqueue_notification(notification(1, final=1000))
    await repo.enqueue_notification(notification(1, final=700))
    old, new = await repo.get_notification(1), await repo.get_notification(2)
    assert old["status"] == "superseded" and new["status"] == "pending"
    sender = Sender()
    await NotificationDispatcher(repo, sender).dispatch()
    assert sender.sent == [1]
    assert (await repo.get_notification(1))["status"] == "superseded"  # kept, not deleted


async def test_duplicate_signature_is_ignored(repo):
    assert await repo.enqueue_notification(notification(1))
    assert not await repo.enqueue_notification(notification(1))
    assert await repo.count_notifications("pending") == 1
