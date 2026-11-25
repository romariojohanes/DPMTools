from concurrent.futures import ThreadPoolExecutor

from core.engine import Run
from core.schema import Settings


def _create_existing_run(tmp_path):
    run = Run.create(
        tmp_path / 'runs',
        Settings(memory_limit='256MB', max_temp_directory_size='1GB'),
    )
    run.meta['status'] = 'COMPLETED'
    run.save()
    run.close()
    return run.directory


def test_existing_run_can_be_opened_repeatedly_without_changing_temp_directory(tmp_path):
    directory = _create_existing_run(tmp_path)

    for _ in range(3):
        with Run(directory) as run:
            assert run.con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(
                (directory / 'temp').resolve()
            )
            assert run.meta['status'] == 'COMPLETED'


def test_existing_run_supports_concurrent_view_and_background_export_connections(tmp_path):
    directory = _create_existing_run(tmp_path)

    with Run(directory) as viewer:
        viewer.con.execute('CREATE TABLE export_source AS SELECT range AS id FROM range(1000)')

        def background_export_access():
            with Run(directory) as exporter:
                return exporter.con.execute('SELECT count(*) FROM export_source').fetchone()[0]

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(background_export_access).result(timeout=10) == 1000
        assert viewer.con.execute('SELECT count(*) FROM export_source').fetchone()[0] == 1000


def test_temp_storage_safeguard_is_applied_at_connection_open(tmp_path):
    directory = _create_existing_run(tmp_path)

    with Run(directory) as run:
        configured = run.con.execute(
            "SELECT current_setting('temp_directory'), current_setting('max_temp_directory_size')"
        ).fetchone()

    assert configured[0] == str((directory / 'temp').resolve())
    # DuckDB normalizes 1 GB to its binary-size display value.
    assert configured[1] == '953.6 MiB'
