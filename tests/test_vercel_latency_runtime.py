from pathlib import Path


# Regression coverage for the canonical v3.3 Telegram/Vercel runtime.
RUNTIME = Path("vercel_runtime/plane_workflows.py")
SUPPORT = Path("vercel_runtime/plane_runtime_support.py")
INGRESS = Path("vercel_runtime/api/index.py")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_telegram_hot_path_does_not_start_scheduler_per_update():
    text = _text(SUPPORT)
    assert "await application.start()" not in text
    assert "await application.stop()" in text  # generation teardown only
    assert "_telegram_application" in text


def test_hot_paths_skip_repeated_index_maintenance():
    support = _text(SUPPORT)
    runtime = _text(RUNTIME)
    assert support.count("ensure_indexes=False") + runtime.count("ensure_indexes=False") >= 2
    assert "ensure_indexes=True" in runtime


def test_http_clients_do_not_log_telegram_token_urls_at_info():
    text = _text(SUPPORT)
    assert '"httpx", "httpcore", "telegram.request"' in text
    assert "setLevel(logging.WARNING)" in text


def test_latency_instrumentation_is_present_without_config_dump():
    support = _text(SUPPORT)
    runtime = _text(RUNTIME)
    ingress = _text(INGRESS)
    assert "source_ms=" in support
    assert "init_ms=" in support
    assert "runtime_ms=" in runtime
    assert "handler_ms=" in runtime
    assert "total_ms=" in runtime
    assert "resume_ms=" in ingress
    assert "logger.info(config" not in support + runtime + ingress


def test_slow_photo_work_is_detached_from_ordered_telegram_queue():
    support = _text(SUPPORT)
    runtime = _text(RUNTIME)
    assert "_should_detach_telegram_update" in support
    assert 'data.startswith("photo:")' in support
    assert '{"/photo", "/conditions"}' in support
    assert "telegram_slow_update_workflow_v33" in runtime
    assert "await start(telegram_slow_update_workflow_v33" in runtime
    assert "await process_telegram_update_v33" in runtime


def test_monitor_and_bootstrap_do_not_close_shared_mongo_client():
    runtime = _text(RUNTIME)
    configure = runtime.split("async def configure_telegram_v33", 1)[1].split("async def process_telegram_update_v33", 1)[0]
    monitor = runtime.split("async def monitor_cycle_step_v33", 1)[1].split("async def chain_monitor_workflow_v33", 1)[0]
    assert "close_db" not in configure
    assert "close_db" not in monitor


def test_v33_callback_is_acknowledged_by_webhook_response():
    text = _text(INGRESS)
    assert 'title="Plane? Telegram Bot v3.3"' in text
    assert '"version": "3.3"' in text
    assert '"method": "answerCallbackQuery"' in text
    assert '"callback_query_id": callback_id' in text
    assert "JSONResponse" in text


def test_v33_handler_does_not_double_answer_silent_callback():
    text = _text(RUNTIME)
    assert "_install_preacked_callback_answer" in text
    assert "if not args and not text and not show_alert and not url" in text
    assert "return await original_answer(self, *args, **kwargs)" in text


def test_v33_normal_and_slow_updates_use_fast_processor():
    text = _text(RUNTIME)
    assert text.count("await process_telegram_update_v33") >= 2
    assert "Telegram v3.3 update processed" in text
