from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from product_ui.data_access import (
    BASE_DIR,
    add_definition_spec,
    build_view_model,
    build_session_list_payload,
    build_definition_template_csv_bytes,
    build_definition_template_xlsx_bytes,
    create_project,
    delete_definition_spec,
    get_project_paths,
    load_session_detail,
    load_session_diagnosis,
    load_results,
    save_settings,
    save_version_note,
    start_session,
    stop_session,
    delete_session,
)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent / "templates"),
    static_folder=str(Path(__file__).resolve().parent / "static"),
)

PRIMARY_MENUS = ["Project", "Versions", "Sessions", "Results", "Settings"]


def _normalize_menu(raw: str) -> str:
    text = str(raw or "").strip()
    if text not in PRIMARY_MENUS:
        return "Sessions"
    return text


def _req_value(name: str, default: str = "") -> str:
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            return str(payload.get(name, default)).strip()
    return str(request.form.get(name, default)).strip()


@app.get("/")
def root() -> str:
    return redirect(url_for("dashboard"))


@app.get("/dashboard")
def dashboard() -> str:
    project = str(request.args.get("project", "")).strip()
    menu = _normalize_menu(str(request.args.get("menu", "Sessions")))
    session_filter = str(request.args.get("run_filter", "all")).strip().lower()
    selected_session_id = str(request.args.get("session_id", "")).strip()
    notice = str(request.args.get("notice", "")).strip()
    error = str(request.args.get("error", "")).strip()
    vm = build_view_model(project_slug=project, session_filter=session_filter)
    session_detail = (
        load_session_detail(vm.get("selected_project", ""), selected_session_id, limit=160)
        if selected_session_id and vm.get("selected_project")
        else {"session_id": "", "summary": {}, "events": [], "suspicion": []}
    )
    return render_template(
        "dashboard.html",
        menu=menu,
        notice=notice,
        error=error,
        selected_session_id=selected_session_id,
        session_detail=session_detail,
        **vm,
    )


@app.get("/api/bootstrap")
def api_bootstrap():
    project = str(request.args.get("project", "")).strip()
    session_filter = str(request.args.get("run_filter", "all")).strip().lower()
    vm = build_view_model(project_slug=project, session_filter=session_filter)
    projects = [
        {
            "slug": p.slug,
            "name": p.name,
            "domain": p.domain,
            "created_at": p.created_at,
        }
        for p in vm.get("projects", [])
    ]
    return jsonify(
        {
            "ok": True,
            "projects": projects,
            "selected_project": vm.get("selected_project"),
            "overview": vm.get("overview", {}),
            "versions": vm.get("versions", []),
            "results": vm.get("results", {}),
            "settings": vm.get("settings", {}),
            "definition_specs": vm.get("definition_specs", []),
            "deleted_versions": vm.get("deleted_versions", []),
            "run_type_labels": vm.get("run_type_labels", {}),
            "session_filter": vm.get("session_filter", "all"),
        }
    )


@app.get("/api/results")
def api_results():
    project = str(request.args.get("project", "")).strip()
    if not project:
        return jsonify({"ok": False, "error": "project is required"}), 400
    return jsonify({"ok": True, "results": load_results(project)})


@app.post("/versions/save")
def save_version() -> str:
    project = _req_value("project")
    version_id = _req_value("version_id")
    if project and version_id:
        save_version_note(
            project_slug=project,
            version_id=version_id,
            display_name=_req_value("display_name"),
            status=_req_value("status"),
            change_reason=_req_value("change_reason"),
            definition_link=_req_value("definition_link"),
            site_change_memo=_req_value("site_change_memo"),
            action=_req_value("action"),
        )
    if request.is_json:
        return jsonify({"ok": True, "project": project, "version_id": version_id})
    return redirect(url_for("dashboard", project=project, menu="Versions"))


@app.post("/settings/save")
def save_settings_route() -> str:
    project = _req_value("project")
    if project:
        save_settings(
            project_slug=project,
            payload={
                "base_domain": _req_value("base_domain"),
                "default_start_url": _req_value("default_start_url"),
                "browser": _req_value("browser", "chromium"),
                "viewport": _req_value("viewport", "Desktop 1440x900"),
                "collection_option": _req_value("collection_option", "Auto Crawl"),
                "judgement_rule": _req_value("judgement_rule", "Network First"),
                "saved_start_pages_text": _req_value("saved_start_pages_text"),
                "scenario_page_groups_text": _req_value("scenario_page_groups_text"),
            },
        )
    if request.is_json:
        return jsonify({"ok": True, "project": project})
    return redirect(url_for("dashboard", project=project, menu="Settings"))


@app.post("/sessions/start")
def start_session_route() -> str:
    project = _req_value("project")
    run_filter = _req_value("run_filter", "all")
    try:
        snapshot = start_session(
            project_slug=project,
            version_id=_req_value("version_id", "ver_1_0") or "ver_1_0",
            run_type=_req_value("run_type", "exploratory") or "exploratory",
            qa_mode=_req_value("qa_mode", "전체 탐색 테스트") or "전체 탐색 테스트",
            tester_name=_req_value("tester_name"),
            start_mode=_req_value("start_mode", "manual"),
            test_scope=_req_value("test_scope", "start_page"),
            start_page_mode=_req_value("start_page_mode", "use_default"),
            start_url=_req_value("start_url"),
            saved_page_name=_req_value("saved_page_name"),
            scenario_group_name=_req_value("scenario_group_name"),
            definition_spec_id=_req_value("definition_spec_id"),
            viewport=_req_value("viewport"),
            analytics_source_mode=_req_value("analytics_source_mode", "both"),
        )
        sid = str(snapshot.get("session_id", "")).strip()
        if request.is_json:
            return jsonify({"ok": True, "session_id": sid, "snapshot": snapshot})
        return redirect(
            url_for(
                "dashboard",
                project=project,
                menu="Sessions",
                run_filter=run_filter,
                notice=f"세션 시작 완료: {sid}",
            )
        )
    except Exception as exc:
        if request.is_json:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return redirect(
            url_for(
                "dashboard",
                project=project,
                menu="Sessions",
                run_filter=run_filter,
                error=f"세션 시작 실패: {str(exc)}",
            )
        )


@app.post("/sessions/stop")
def stop_session_route() -> str:
    project = _req_value("project")
    run_filter = _req_value("run_filter", "all")
    session_id = _req_value("session_id")
    try:
        snapshot = stop_session(project_slug=project, session_id=session_id)
        if request.is_json:
            return jsonify({"ok": True, "session_id": session_id, "snapshot": snapshot})
        return redirect(
            url_for(
                "dashboard",
                project=project,
                menu="Sessions",
                run_filter=run_filter,
                notice=f"세션 종료 요청 완료: {session_id}",
                session_id=session_id,
            )
        )
    except Exception as exc:
        if request.is_json:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return redirect(
            url_for(
                "dashboard",
                project=project,
                menu="Sessions",
                run_filter=run_filter,
                error=f"세션 종료 실패: {str(exc)}",
                session_id=session_id,
            )
        )


@app.post("/sessions/delete")
def delete_session_route() -> str:
    project = _req_value("project")
    run_filter = _req_value("run_filter", "all")
    session_id = _req_value("session_id")
    try:
        snapshot = delete_session(project_slug=project, session_id=session_id)
        if not bool(snapshot.get("ok", False)):
            raise ValueError(str(snapshot.get("error", "세션 삭제 실패")))
        if request.is_json:
            return jsonify({"ok": True, "session_id": session_id, "snapshot": snapshot})
        return redirect(
            url_for(
                "dashboard",
                project=project,
                menu="Sessions",
                run_filter=run_filter,
                notice=f"세션 삭제 완료: {session_id}",
            )
        )
    except Exception as exc:
        if request.is_json:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return redirect(
            url_for(
                "dashboard",
                project=project,
                menu="Sessions",
                run_filter=run_filter,
                error=f"세션 삭제 실패: {str(exc)}",
            )
        )


@app.get("/api/sessions")
def api_sessions():
    project = str(request.args.get("project", "")).strip()
    run_filter = str(request.args.get("run_filter", "all")).strip().lower()
    if not project:
        return jsonify({"ok": False, "error": "project is required"}), 400
    payload = build_session_list_payload(project_slug=project, session_filter=run_filter)
    return jsonify({"ok": True, **payload})


@app.get("/api/session_detail")
def api_session_detail():
    project = str(request.args.get("project", "")).strip()
    session_id = str(request.args.get("session_id", "")).strip()
    if not project or not session_id:
        return jsonify({"ok": False, "error": "project and session_id are required"}), 400
    detail = load_session_detail(project_slug=project, session_id=session_id, limit=160)
    return jsonify({"ok": True, **detail})


@app.get("/api/session_diagnose")
def api_session_diagnose():
    project = str(request.args.get("project", "")).strip()
    session_id = str(request.args.get("session_id", "")).strip()
    if not project or not session_id:
        return jsonify({"ok": False, "error": "project and session_id are required"}), 400
    try:
        diagnosis = load_session_diagnosis(project_slug=project, session_id=session_id, limit=5000)
    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "session_id": session_id,
                    "error": f"session_diagnose_failed: {str(exc)}",
                }
            ),
            500,
        )
    status = 200 if bool(diagnosis.get("ok", False)) else 400
    return jsonify(diagnosis), status


@app.get("/download")
def download_export_file():
    project = str(request.args.get("project", "")).strip()
    rel_path = str(request.args.get("path", "")).strip()
    if not project or not rel_path:
        abort(400)

    full_path = (BASE_DIR / rel_path).resolve()
    project_root = get_project_paths(project).get("root", BASE_DIR).resolve()
    if not str(full_path).startswith(str(project_root)):
        abort(403)
    if not full_path.exists() or not full_path.is_file():
        abort(404)
    return send_file(full_path, as_attachment=True, download_name=full_path.name)


@app.post("/definitions/upload")
def upload_definition():
    project = _req_value("project")
    if not project:
        return jsonify({"ok": False, "error": "project is required"}), 400
    f = request.files.get("definition_file")
    if f is None:
        return jsonify({"ok": False, "error": "definition_file is required"}), 400
    try:
        content = f.read()
        if not content:
            return jsonify({"ok": False, "error": "빈 파일은 업로드할 수 없습니다."}), 400
        item = add_definition_spec(project_slug=project, original_name=f.filename or "definition.csv", content=content)
        return jsonify({"ok": True, "item": item})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/definitions/delete")
def delete_definition():
    project = _req_value("project")
    spec_id = _req_value("spec_id")
    if not project or not spec_id:
        return jsonify({"ok": False, "error": "project and spec_id are required"}), 400
    ok = delete_definition_spec(project_slug=project, spec_id=spec_id)
    return jsonify({"ok": ok})


@app.get("/definitions/template")
def download_definition_template():
    file_format = str(request.args.get("format", "xlsx")).strip().lower()
    if file_format == "csv":
        content = build_definition_template_csv_bytes()
        return send_file(
            BytesIO(content),
            as_attachment=True,
            download_name="definition_template.csv",
            mimetype="text/csv; charset=utf-8",
        )
    content = build_definition_template_xlsx_bytes()
    return send_file(
        BytesIO(content),
        as_attachment=True,
        download_name="definition_template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/projects/create")
def create_project_route():
    name = _req_value("name")
    domain = _req_value("domain")
    try:
        info = create_project(name=name, domain=domain)
        return jsonify(
            {
                "ok": True,
                "project": {
                    "slug": info.slug,
                    "name": info.name,
                    "domain": info.domain,
                    "created_at": info.created_at,
                },
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


if __name__ == "__main__":
    debug_mode = str(os.environ.get("PRODUCT_UI_DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=8610, debug=debug_mode, use_reloader=debug_mode)
