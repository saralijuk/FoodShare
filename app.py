
"""Food Waste Management & Donation System - Flask app.

Layers:
  templates/*.html -> Frontend
  app.py           -> Business logic and validation
  db.py            -> Oracle connectivity

Donation status flow:
  AVAILABLE -> ACCEPTED -> PICKED_UP -> DISTRIBUTED
"""

import os
import math
from datetime import datetime

import oracledb
from flask import Flask, flash, redirect, render_template, request, url_for

import db

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")

DT_FORMAT = "%Y-%m-%dT%H:%M"

DONOR_TYPES = ["RESTAURANT", "INDIVIDUAL", "EVENT_ORGANISER"]
UNITS = ["kg", "g", "litre", "plates", "packets", "pieces"]


# ---------------------------------------------------------------- helpers

def parse_dt(text):
    try:
        return datetime.strptime(text, DT_FORMAT)
    except (ValueError, TypeError):
        return None


def time_left(expiry):
    """Calculate the time remaining until expiry."""
    secs = int((expiry - datetime.now()).total_seconds())

    if secs <= 0:
        return "Expired"

    hours, mins = divmod(secs // 60, 60)
    return f"{hours}h {mins}m"


@app.template_filter("fmt")
def fmt(value):
    return value.strftime("%d %b %Y, %I:%M %p") if value else "-"


app.jinja_env.globals["time_left"] = time_left


@app.template_filter("label")
def label(value):
    """Convert database status codes into readable labels."""
    return value.replace("_", " ").title() if value else "-"


@app.template_filter("cls")
def css_class(value):
    """Convert a status into a CSS-friendly class."""
    return value.replace("_", "").replace(" ", "").lower() if value else ""


def pickup_stage(row):
    """Derive the pickup stage from its status and picked_time."""
    if row.get("status") == "COMPLETED":
        return "Completed"

    return "Picked Up" if row.get("picked_time") else "Scheduled"


@app.errorhandler(oracledb.Error)
def db_error(err):
    # Keep database details out of the page shown to users.
    app.logger.exception("Oracle database error: %s", err)
    return render_template(
        "error.html",
        message="A database error occurred. Please try again."
    ), 500


# ---------------------------------------------------------------- home

@app.route("/")
def index():
    stats = {
        "Donors": db.scalar("SELECT COUNT(*) FROM DONOR"),
        "NGOs": db.scalar("SELECT COUNT(*) FROM NGO"),
        "Available donations": db.scalar(
            "SELECT COUNT(*) FROM DONATION WHERE status = 'AVAILABLE'"
        ),
        "Distributed": db.scalar(
            "SELECT COUNT(*) FROM DONATION WHERE status = 'DISTRIBUTED'"
        ),
        "People served": db.scalar(
            "SELECT NVL(SUM(people_served), 0) FROM DISTRIBUTION"
        ),
    }

    return render_template("index.html", stats=stats)


# ---------------------------------------------------------------- donors

@app.route("/donor/register", methods=["GET", "POST"])
def donor_register():
    if request.method == "POST":
        f = {
            k: request.form.get(k, "").strip()
            for k in (
                "donor_name", "donor_type", "phone", "email",
                "address", "city", "fssai_no"
            )
        }

        errors = []

        if not f["donor_name"]:
            errors.append("Name is required.")

        if f["donor_type"] not in DONOR_TYPES:
            errors.append("Choose a valid donor type.")

        if not (f["phone"].isdigit() and len(f["phone"]) == 10):
            errors.append("Phone must be exactly 10 digits.")

        if "@" not in f["email"] or "." not in f["email"]:
            errors.append("Enter a valid email address.")

        if not f["city"]:
            errors.append("City is required.")

        if f["donor_type"] == "RESTAURANT" and not f["fssai_no"]:
            errors.append("FSSAI number is required for restaurants.")

        if f["donor_type"] != "RESTAURANT":
            f["fssai_no"] = ""

        if not errors and db.scalar(
            "SELECT COUNT(*) FROM DONOR WHERE email = :e",
            {"e": f["email"]}
        ):
            errors.append("A donor with this email already exists.")

        if errors:
            for error in errors:
                flash(error, "error")

            return render_template(
                "donor_register.html",
                types=DONOR_TYPES,
                f=f
            )

        donor_id = db.next_id("DONOR", "donor_id")

        db.execute(
            """INSERT INTO DONOR (
                   donor_id, donor_name, donor_type, phone,
                   email, address, city, fssai_no
               )
               VALUES (
                   :id, :name, :type, :phone,
                   :email, :addr, :city, :fssai
               )""",
            {
                "id": donor_id,
                "name": f["donor_name"],
                "type": f["donor_type"],
                "phone": f["phone"],
                "email": f["email"],
                "addr": f["address"],
                "city": f["city"],
                "fssai": f["fssai_no"] or None,
            }
        )

        flash(
            f"Donor registered successfully (ID {donor_id}).",
            "success"
        )
        return redirect(url_for("donate"))

    return render_template(
        "donor_register.html",
        types=DONOR_TYPES,
        f={}
    )


# ---------------------------------------------------------------- donation

@app.route("/donate", methods=["GET", "POST"])
def donate():
    donors = db.query(
        "SELECT donor_id, donor_name, donor_type FROM DONOR "
        "ORDER BY donor_name"
    )

    cats = db.query(
        "SELECT category_id, category_name FROM FOOD_CATEGORY "
        "ORDER BY category_name"
    )

    if request.method == "POST":
        donor_id = request.form.get("donor_id", "")
        location = request.form.get("pickup_location", "").strip()

        names = request.form.getlist("food_name")
        cat_ids = request.form.getlist("category_id")
        qtys = request.form.getlist("quantity")
        units = request.form.getlist("unit")
        expiries = request.form.getlist("expiry_time")

        errors = []
        items = []

        # Validate the donor.
        if not donor_id.isdigit():
            errors.append("Select a valid donor.")
        elif not db.scalar(
            "SELECT COUNT(*) FROM DONOR WHERE donor_id = :d",
            {"d": int(donor_id)}
        ):
            errors.append("Select a valid donor.")

        if not location:
            errors.append("Pickup location is required.")

        # Validate each food item independently.
        for i, raw_name in enumerate(names):
            name = raw_name.strip()

            # Ignore a completely empty food-name row.
            if not name:
                continue

            row = i + 1

            # Check that every required field exists.
            if (
                i >= len(qtys)
                or i >= len(cat_ids)
                or i >= len(units)
                or i >= len(expiries)
            ):
                errors.append(f"Item {row}: incomplete food item.")
                continue

            item_valid = True

            # Validate quantity.
            try:
                qty = float(qtys[i])
                if not math.isfinite(qty):
                    qty = 0
            except (ValueError, TypeError):
                qty = 0

            if qty <= 0:
                errors.append(
                    f"Item {row}: quantity must be greater than 0."
                )
                item_valid = False

            # Validate expiry.
            expiry = parse_dt(expiries[i])

            if expiry is None:
                errors.append(
                    f"Item {row}: enter an expiry date and time."
                )
                item_valid = False
            elif expiry <= datetime.now():
                errors.append(
                    f"Item {row}: expiry must be in the future."
                )
                item_valid = False

            # Validate the category format and existence.
            if not cat_ids[i].isdigit():
                errors.append(f"Item {row}: choose a category.")
                item_valid = False
            elif not db.scalar(
                "SELECT COUNT(*) FROM FOOD_CATEGORY "
                "WHERE category_id = :c",
                {"c": int(cat_ids[i])}
            ):
                errors.append(
                    f"Item {row}: the selected category does not exist."
                )
                item_valid = False

            # Validate the unit.
            if units[i] not in UNITS:
                errors.append(f"Item {row}: choose a valid unit.")
                item_valid = False

            # IMPORTANT: append based on this item's validation,
            # not on errors belonging to the entire form.
            if item_valid:
                items.append(
                    (name, int(cat_ids[i]), qty, units[i], expiry)
                )

        # Require at least one named food item.
        if not items and not errors:
            errors.append("Add at least one food item.")

        # Reject the entire form if any validation error exists.
        if errors:
            for error in errors:
                flash(error, "error")

            return render_template(
                "donate.html",
                donors=donors,
                cats=cats,
                units=UNITS,
                form=request.form
            )

        # Create the donation and all its items in one transaction.
        did = db.next_id("DONATION", "donation_id")

        stmts = [
            (
                """INSERT INTO DONATION (
                       donation_id, donor_id, pickup_location,
                       posted_at, status
                   )
                   VALUES (
                       :id, :donor, :loc, :posted, 'AVAILABLE'
                   )""",
                {
                    "id": did,
                    "donor": int(donor_id),
                    "loc": location,
                    "posted": datetime.now(),
                }
            )
        ]

        for item_no, (name, cat, qty, unit, expiry) in enumerate(
            items, start=1
        ):
            stmts.append(
                (
                    """INSERT INTO DONATION_ITEM (
                           donation_id, item_no, food_name,
                           quantity, unit, expiry_time, category_id
                       )
                       VALUES (
                           :d, :n, :food, :q, :u, :exp, :cat
                       )""",
                    {
                        "d": did,
                        "n": item_no,
                        "food": name,
                        "q": qty,
                        "u": unit,
                        "exp": expiry,
                        "cat": cat,
                    }
                )
            )

        db.transaction(stmts)

        flash(
            f"Donation #{did} posted with {len(items)} item(s).",
            "success"
        )
        return redirect(url_for("available"))

    return render_template(
        "donate.html",
        donors=donors,
        cats=cats,
        units=UNITS,
        form=request.form
    )


# ---------------------------------------------------------------- NGO side

@app.route("/available")
def available():
    """Show available donations whose earliest expiry is in the future."""
    donations = db.query(
        """SELECT d.donation_id, d.pickup_location, d.posted_at,
                  dn.donor_name, dn.donor_type, dn.city,
                  MIN(i.expiry_time) AS earliest_expiry
           FROM DONATION d
           JOIN DONOR dn ON dn.donor_id = d.donor_id
           JOIN DONATION_ITEM i ON i.donation_id = d.donation_id
           WHERE d.status = 'AVAILABLE'
           GROUP BY d.donation_id, d.pickup_location, d.posted_at,
                    dn.donor_name, dn.donor_type, dn.city
           HAVING MIN(i.expiry_time) > :now
           ORDER BY earliest_expiry""",
        {"now": datetime.now()}
    )

    items = db.query(
        """SELECT i.donation_id, i.food_name, i.quantity, i.unit,
                  c.category_name
           FROM DONATION_ITEM i
           JOIN FOOD_CATEGORY c ON c.category_id = i.category_id
           ORDER BY i.donation_id, i.item_no"""
    )

    by_donation = {}

    for item in items:
        by_donation.setdefault(item["donation_id"], []).append(item)

    return render_template(
        "available.html",
        donations=donations,
        items=by_donation
    )


@app.route("/accept/<int:donation_id>", methods=["GET", "POST"])
def accept(donation_id):
    donation = db.query_one(
        """SELECT d.donation_id, d.status, d.pickup_location,
                  dn.donor_name,
                  (SELECT MIN(expiry_time)
                   FROM DONATION_ITEM
                   WHERE donation_id = d.donation_id) AS earliest_expiry
           FROM DONATION d
           JOIN DONOR dn ON dn.donor_id = d.donor_id
           WHERE d.donation_id = :id""",
        {"id": donation_id}
    )

    if donation is None:
        flash("Donation not found.", "error")
        return redirect(url_for("available"))

    ngos = db.query("SELECT ngo_id, name FROM NGO ORDER BY name")

    vols = db.query(
        """SELECT v.volunteer_id, v.name, v.ngo_id,
                  n.name AS ngo_name
           FROM VOLUNTEER v
           JOIN NGO n ON n.ngo_id = v.ngo_id
           ORDER BY n.name, v.name"""
    )

    if request.method == "POST":
        ngo_id = request.form.get("ngo_id", "")
        vol_id = request.form.get("volunteer_id", "")
        sched = parse_dt(request.form.get("scheduled_time"))

        errors = []

        if donation["status"] != "AVAILABLE":
            errors.append(
                f"This donation is already {label(donation['status'])}."
            )

        if donation["earliest_expiry"] is None:
            errors.append("This donation has no valid food expiry.")
        elif donation["earliest_expiry"] <= datetime.now():
            errors.append(
                "This donation has expired and cannot be accepted."
            )

        if not ngo_id.isdigit() or not vol_id.isdigit():
            errors.append("Select an NGO and a volunteer.")
        elif not db.scalar(
            """SELECT COUNT(*) FROM VOLUNTEER
               WHERE volunteer_id = :v AND ngo_id = :n""",
            {"v": int(vol_id), "n": int(ngo_id)}
        ):
            errors.append(
                "That volunteer does not belong to the selected NGO."
            )

        if sched is None:
            errors.append("Enter a scheduled pickup time.")
        elif sched < datetime.now().replace(second=0, microsecond=0):
            errors.append("Pickup time cannot be in the past.")
        elif (
            donation["earliest_expiry"] is not None
            and sched >= donation["earliest_expiry"]
        ):
            errors.append(
                "Pickup must be scheduled before the food expires."
            )

        if errors:
            for error in errors:
                flash(error, "error")

            return render_template(
                "accept.html",
                d=donation,
                ngos=ngos,
                vols=vols
            )

        pid = db.next_id("PICKUP", "pickup_id")

        try:
            db.transaction([
                (
                    """INSERT INTO PICKUP (
                           pickup_id, donation_id, ngo_id,
                           volunteer_id, scheduled_time,
                           picked_time, status
                       )
                       VALUES (
                           :p, :d, :n, :v, :s, NULL, 'SCHEDULED'
                       )""",
                    {
                        "p": pid,
                        "d": donation_id,
                        "n": int(ngo_id),
                        "v": int(vol_id),
                        "s": sched,
                    }
                ),
                (
                    """UPDATE DONATION
                       SET status = 'ACCEPTED'
                       WHERE donation_id = :d
                       AND status = 'AVAILABLE'""",
                    {"d": donation_id}
                ),
            ])
        except oracledb.IntegrityError:
            flash(
                "Another NGO just accepted this donation.",
                "error"
            )
            return redirect(url_for("available"))

        flash(
            f"Donation #{donation_id} accepted. Pickup #{pid} scheduled.",
            "success"
        )
        return redirect(url_for("pickups"))

    return render_template(
        "accept.html",
        d=donation,
        ngos=ngos,
        vols=vols
    )


# ---------------------------------------------------------------- pickups

@app.route("/pickups")
def pickups():
    rows = db.query(
        """SELECT p.pickup_id, p.donation_id, p.scheduled_time,
                  p.picked_time, p.status,
                  n.name AS ngo_name, v.name AS volunteer_name,
                  d.pickup_location, dn.donor_name
           FROM PICKUP p
           JOIN NGO n ON n.ngo_id = p.ngo_id
           JOIN VOLUNTEER v ON v.volunteer_id = p.volunteer_id
           JOIN DONATION d ON d.donation_id = p.donation_id
           JOIN DONOR dn ON dn.donor_id = d.donor_id
           ORDER BY p.pickup_id DESC"""
    )

    for row in rows:
        row["stage"] = pickup_stage(row)

    return render_template("pickups.html", rows=rows)


@app.route("/pickup/<int:pickup_id>/picked", methods=["POST"])
def mark_picked(pickup_id):
    pk = db.query_one(
        """SELECT donation_id, status, scheduled_time, picked_time
           FROM PICKUP
           WHERE pickup_id = :p""",
        {"p": pickup_id}
    )

    if pk is None or pickup_stage(pk) != "Scheduled":
        flash(
            "Only a scheduled pickup can be marked as picked up.",
            "error"
        )
        return redirect(url_for("pickups"))

    if datetime.now() < pk["scheduled_time"]:
        flash(
            "Too early: pickup was scheduled for "
            f"{fmt(pk['scheduled_time'])}.",
            "error"
        )
        return redirect(url_for("pickups"))

    db.transaction([
        (
            """UPDATE PICKUP
               SET picked_time = :t
               WHERE pickup_id = :p
               AND picked_time IS NULL
               AND status = 'SCHEDULED'""",
            {"t": datetime.now(), "p": pickup_id}
        ),
        (
            """UPDATE DONATION
               SET status = 'PICKED_UP'
               WHERE donation_id = :d
               AND status = 'ACCEPTED'""",
            {"d": pk["donation_id"]}
        ),
    ])

    flash(f"Pickup #{pickup_id} marked as picked up.", "success")
    return redirect(url_for("pickups"))


# ---------------------------------------------------------------- distribution

@app.route("/distribute/<int:pickup_id>", methods=["GET", "POST"])
def distribute(pickup_id):
    pk = db.query_one(
        """SELECT p.pickup_id, p.donation_id, p.status,
                  p.picked_time, n.name AS ngo_name
           FROM PICKUP p
           JOIN NGO n ON n.ngo_id = p.ngo_id
           WHERE p.pickup_id = :p""",
        {"p": pickup_id}
    )

    if pk is None:
        flash("Pickup not found.", "error")
        return redirect(url_for("pickups"))

    pk["stage"] = pickup_stage(pk)

    if request.method == "POST":
        area = request.form.get("area", "").strip()
        people = request.form.get("people_served", "")

        errors = []

        if pk["stage"] != "Picked Up":
            errors.append(
                "Food must be picked up before it is distributed."
            )

        if not area:
            errors.append("Area is required.")

        if not people.isdigit() or int(people) <= 0:
            errors.append(
                "People served must be a whole number above 0."
            )

        if db.scalar(
            "SELECT COUNT(*) FROM DISTRIBUTION WHERE pickup_id = :p",
            {"p": pickup_id}
        ):
            errors.append(
                "Distribution already recorded for this pickup."
            )

        if errors:
            for error in errors:
                flash(error, "error")

            return render_template("distribute.html", pk=pk)

        did = db.next_id("DISTRIBUTION", "distribution_id")

        db.transaction([
            (
                """INSERT INTO DISTRIBUTION (
                       distribution_id, pickup_id,
                       distribution_date, area, people_served
                   )
                   VALUES (:d, :p, :dt, :a, :n)""",
                {
                    "d": did,
                    "p": pickup_id,
                    "dt": datetime.now(),
                    "a": area,
                    "n": int(people),
                }
            ),
            (
                """UPDATE PICKUP
                   SET status = 'COMPLETED'
                   WHERE pickup_id = :p
                   AND status = 'SCHEDULED'
                   AND picked_time IS NOT NULL""",
                {"p": pickup_id}
            ),
            (
                """UPDATE DONATION
                   SET status = 'DISTRIBUTED'
                   WHERE donation_id = :d
                   AND status = 'PICKED_UP'""",
                {"d": pk["donation_id"]}
            ),
        ])

        flash(
            "Distribution recorded. Donation complete!",
            "success"
        )
        return redirect(url_for("track"))

    return render_template("distribute.html", pk=pk)


# ---------------------------------------------------------------- tracking

@app.route("/track")
def track():
    """Trace a donation from donor through to distribution."""
    rows = db.query(
        """SELECT d.donation_id, d.status, d.posted_at,
                  dn.donor_name, n.name AS ngo_name,
                  v.name AS volunteer_name, p.picked_time,
                  ds.area, ds.people_served
           FROM DONATION d
           JOIN DONOR dn ON dn.donor_id = d.donor_id
           LEFT JOIN PICKUP p ON p.donation_id = d.donation_id
           LEFT JOIN NGO n ON n.ngo_id = p.ngo_id
           LEFT JOIN VOLUNTEER v ON v.volunteer_id = p.volunteer_id
           LEFT JOIN DISTRIBUTION ds ON ds.pickup_id = p.pickup_id
           ORDER BY d.donation_id DESC"""
    )

    return render_template("track.html", rows=rows)


if __name__ == "__main__":
    app.run(debug=True)
