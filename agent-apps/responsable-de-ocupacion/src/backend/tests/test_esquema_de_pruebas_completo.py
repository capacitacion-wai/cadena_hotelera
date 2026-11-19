"""The test schema has EVERY skill-owned table, whatever the employee's `app_models/__init__.py`
lists (it is scaffold: an employee created before a model existed does not import it). Run
alone, without importing the modules that use those tables: that is exactly the situation of
an evolved employee's suite (14.14: `push_devices` missing, five unrelated tests failed)."""


def test_el_esquema_de_pruebas_tiene_las_tablas_del_skill():
    from app.app_database import AppBase
    tablas = set(AppBase.metadata.tables)
    assert {"push_devices", "approval_notices"} <= tablas
