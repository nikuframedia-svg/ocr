from __future__ import annotations

from app.web.main import templates


def _render_cell(
    field_path: str,
    status: str,
    *,
    ref: str = "",
    ref_title: str = "",
) -> str:
    return templates.env.get_template("_cell.html").render(
        audit=None,
        field_path=field_path,
        value="1000",
        edited=False,
        sheet_id=1,
        sheet_status="extracted",
        cc_status_by_path={field_path: status},
        cc_ref_by_path={field_path: ref} if ref else {},
        cc_ref_title_by_path={field_path: ref_title} if ref_title else {},
        cc_suspended_by_path={},
        cc_snapped_by_path={},
        cc_obra_concluida_by_path={},
        oob=False,
    )


def test_laser_dbase_dtopo_no_match_render_as_dimension_warning():
    for field in ("dbase", "dtopo"):
        html = _render_cell(f"rows[0].{field}", "NO_MATCH")

        assert "cc-warn" in html
        assert "cc-no-match" not in html


def test_no_match_ref_tooltip_uses_real_reference_source():
    html = _render_cell(
        "header.cod_maquina",
        "NO_MATCH",
        ref="M032",
        ref_title="Máquina esperada: M032",
    )

    assert 'title="Máquina esperada: M032"' in html
    assert "Plan diz: M032" not in html
