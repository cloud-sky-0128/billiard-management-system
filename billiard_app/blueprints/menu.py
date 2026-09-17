from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..db import get_db
from ..money import to_cents

bp = Blueprint("menu", __name__)

@bp.route("/menu")
def menu_page():
    db = get_db()
    categories = db.execute(
        "SELECT * FROM categories WHERE is_active = 1 ORDER BY id"
    ).fetchall()
    items = db.execute(
        """
        SELECT m.*, c.name AS category_name
        FROM menu_items m
        JOIN categories c ON c.id = m.category_id
        WHERE m.is_active = 1 AND c.is_active = 1
        ORDER BY c.id, m.id
        """
    ).fetchall()
    return render_template("menu.html", categories=categories, items=items)


@bp.route("/menu/categories/add", methods=["POST"])
def add_category():
    name = request.form.get("name", "").strip()
    if not name:
        flash("類別名稱不可空白。", "error")
        return redirect(url_for(".menu_page"))

    db = get_db()
    existing = db.execute("SELECT * FROM categories WHERE name = ?", (name,)).fetchone()
    if existing:
        if int(existing["is_active"]) == 1:
            flash("類別已存在。", "error")
            return redirect(url_for(".menu_page"))
        db.execute("UPDATE categories SET is_active = 1 WHERE id = ?", (existing["id"],))
        db.commit()
        flash(f"已重新啟用類別：{name}", "success")
        return redirect(url_for(".menu_page"))

    db.execute("INSERT INTO categories (name, is_active) VALUES (?, 1)", (name,))
    db.commit()
    flash(f"已新增類別：{name}", "success")
    return redirect(url_for(".menu_page"))


@bp.route("/menu/categories/<int:category_id>/delete", methods=["POST"])
def delete_category(category_id: int):
    db = get_db()
    category = db.execute(
        "SELECT * FROM categories WHERE id = ? AND is_active = 1",
        (category_id,),
    ).fetchone()
    if not category:
        flash("找不到可刪除的類別。", "error")
        return redirect(url_for(".menu_page"))

    deleted_items = db.execute(
        "SELECT COUNT(*) AS c FROM menu_items WHERE category_id = ? AND is_active = 1",
        (category_id,),
    ).fetchone()["c"]
    db.execute("UPDATE categories SET is_active = 0 WHERE id = ?", (category_id,))
    db.execute("UPDATE menu_items SET is_active = 0 WHERE category_id = ?", (category_id,))
    db.commit()

    flash(f"已刪除類別：{category['name']}（同步隱藏 {deleted_items} 個品項）", "success")
    return redirect(url_for(".menu_page"))


@bp.route("/menu/items/add", methods=["POST"])
def add_menu_item():
    db = get_db()
    name = request.form.get("name", "").strip()

    try:
        category_id = int(request.form.get("category_id", "0"))
        price_cents = to_cents(request.form.get("price", "0"))
    except ValueError:
        flash("品項資料格式錯誤。", "error")
        return redirect(url_for(".menu_page"))

    if not name:
        flash("品項名稱不可空白。", "error")
        return redirect(url_for(".menu_page"))

    if price_cents <= 0:
        flash("價格需大於 0。", "error")
        return redirect(url_for(".menu_page"))

    category = db.execute(
        "SELECT * FROM categories WHERE id = ? AND is_active = 1",
        (category_id,),
    ).fetchone()
    if not category:
        flash("找不到類別。", "error")
        return redirect(url_for(".menu_page"))

    db.execute(
        "INSERT INTO menu_items (category_id, name, price_cents) VALUES (?, ?, ?)",
        (category_id, name, price_cents),
    )
    db.commit()

    flash(f"已新增品項：{name}", "success")
    return redirect(url_for(".menu_page"))


@bp.route("/menu/items/<int:item_id>/update", methods=["POST"])
def update_menu_item(item_id: int):
    db = get_db()
    name = request.form.get("name", "").strip()
    try:
        category_id = int(request.form.get("category_id", "0"))
        price_cents = to_cents(request.form.get("price", "0"))
    except ValueError:
        flash("品項資料格式錯誤。", "error")
        return redirect(url_for(".menu_page"))

    if not name:
        flash("品項名稱不可空白。", "error")
        return redirect(url_for(".menu_page"))
    if price_cents <= 0:
        flash("價格需大於 0。", "error")
        return redirect(url_for(".menu_page"))

    item = db.execute(
        "SELECT * FROM menu_items WHERE id = ? AND is_active = 1", (item_id,)
    ).fetchone()
    if not item:
        flash("找不到品項。", "error")
        return redirect(url_for(".menu_page"))
    category = db.execute(
        "SELECT * FROM categories WHERE id = ? AND is_active = 1",
        (category_id,),
    ).fetchone()
    if not category:
        flash("找不到類別。", "error")
        return redirect(url_for(".menu_page"))

    db.execute(
        """
        UPDATE menu_items
        SET category_id = ?, name = ?, price_cents = ?
        WHERE id = ?
        """,
        (category_id, name, price_cents, item_id),
    )
    db.commit()
    flash("品項已更新。", "success")
    return redirect(url_for(".menu_page"))


@bp.route("/menu/items/<int:item_id>/delete", methods=["POST"])
def delete_menu_item(item_id: int):
    db = get_db()
    item = db.execute(
        "SELECT * FROM menu_items WHERE id = ? AND is_active = 1",
        (item_id,),
    ).fetchone()
    if not item:
        flash("找不到可刪除的品項。", "error")
        return redirect(url_for(".menu_page"))

    db.execute("UPDATE menu_items SET is_active = 0 WHERE id = ?", (item_id,))
    db.commit()
    flash("品項已刪除。", "success")
    return redirect(url_for(".menu_page"))
