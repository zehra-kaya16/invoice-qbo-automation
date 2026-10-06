from app.tasks import celery_app


def test_celery_app_is_configured():
    assert celery_app.main == "receipt_ai"

    assert (
        celery_app.conf.task_serializer
        == "json"
    )

    assert (
        celery_app.conf.result_serializer
        == "json"
    )

    assert (
        celery_app.conf.accept_content
        == ["json"]
    )

    assert (
        celery_app.conf.task_track_started
        is True
    )

    assert (
        celery_app.conf.task_time_limit
        == 300
    )

    assert (
        celery_app.conf.task_soft_time_limit
        == 240
    )