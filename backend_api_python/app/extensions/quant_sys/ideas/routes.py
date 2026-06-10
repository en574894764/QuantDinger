"""Ideas API routes — submit, list, get, update status."""

from flask import jsonify, request

from app.extensions.quant_sys.ideas import ideas_bp
from app.extensions.quant_sys.ideas.pool import (
    submit_idea,
    list_ideas,
    get_idea,
    update_idea_status,
    get_idea_stats,
)


@ideas_bp.route("", methods=["POST"])
def ideas_submit():
    """Submit a new investment idea.

    JSON body:
        description (required): One-line description.
        logic: Investment logic.
        hypothesis: Testable proposition.
        market: ``a_shares``, ``us_stocks``, ``crypto`` (default: a_shares).
        priority: ``high``, ``medium``, ``low`` (default: medium).
        tags: Comma-separated tags.
    """
    body = request.get_json(silent=True) or {}

    description = (body.get("description") or "").strip()
    if not description:
        return jsonify({"success": False, "error": "description is required"}), 400

    try:
        idea = submit_idea(
            description=description,
            logic=(body.get("logic") or "").strip(),
            hypothesis=(body.get("hypothesis") or "").strip(),
            market=(body.get("market") or "a_shares").strip(),
            priority=(body.get("priority") or "medium").strip(),
            tags=(body.get("tags") or "").strip(),
        )
        return jsonify({"success": True, "data": idea}), 201
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@ideas_bp.route("", methods=["GET"])
def ideas_list():
    """List ideas, optionally filtered by status.

    Query params:
        limit  — max results (default 30).
        status — filter by status (e.g. ``submitted``, ``validated``).
    """
    limit = request.args.get("limit", 30, type=int)
    status = request.args.get("status", "").strip()
    data = list_ideas(limit=limit, status=status)
    return jsonify({"count": len(data), "data": data})


@ideas_bp.route("/stats", methods=["GET"])
def ideas_stats():
    """Aggregated statistics across all ideas."""
    stats = get_idea_stats()
    return jsonify(stats)


@ideas_bp.route("/<idea_id>", methods=["GET"])
def ideas_get(idea_id: str):
    """Get a single idea by ID."""
    idea = get_idea(idea_id)
    if idea is None:
        return jsonify({"success": False, "error": f"Idea not found: {idea_id}"}), 404
    return jsonify({"success": True, "data": idea})


@ideas_bp.route("/<idea_id>/status", methods=["PUT"])
def ideas_update_status(idea_id: str):
    """Update an idea's status (state-machine enforced).

    JSON body:
        status (required): Target status (submitted|researching|backtesting|validated|rejected).
    """
    body = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").strip()

    if not new_status:
        return jsonify({"success": False, "error": "status is required"}), 400

    result = update_idea_status(idea_id, new_status)
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code
