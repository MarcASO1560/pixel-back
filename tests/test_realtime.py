import asyncio
from uuid import uuid4

from app.realtime import RealtimeBroker


def test_realtime_broker_delivers_user_scoped_events() -> None:
    async def scenario() -> None:
        broker = RealtimeBroker()
        user_id = uuid4()
        other_user_id = uuid4()
        subscription = broker.subscribe(user_id=user_id, loop=asyncio.get_running_loop())
        other_subscription = broker.subscribe(
            user_id=other_user_id,
            loop=asyncio.get_running_loop(),
        )

        broker.publish(
            user_ids={user_id},
            event="project.updated",
            data={"project_id": "project-1"},
        )

        event = await subscription.get(timeout_seconds=1)
        other_event = await other_subscription.get(timeout_seconds=0.01)

        assert event is not None
        assert event.event == "project.updated"
        assert event.data == {"project_id": "project-1"}
        assert other_event is None

        subscription.close()
        broker.publish(
            user_ids={user_id},
            event="project.updated",
            data={"project_id": "project-2"},
        )

        assert await subscription.get(timeout_seconds=0.01) is None

    asyncio.run(scenario())
