import flask
from twilio import twiml
import twilio.util
import logging
import smtplib
import time
import re
import random
import datetime
import raven.flask_glue
import psycopg2
import psycopg2.errorcodes
from psycopg2.extras import DateTimeRange, RealDictCursor

from flask import request, url_for, redirect, render_template, \
                  Markup, jsonify, abort, flash, session, g

app = flask.Flask(__name__)


## Setup

twilio_validator = None
raven_decorator = None

@app.before_first_request
def setup_configured_globals():
    """Creates the Twilio RequestValidator and the raven AuthDecorator"""

    global twilio_validator, raven_decorator

    twilio_validator = \
            twilio.util.RequestValidator(app.config["TWILIO_AUTH_TOKEN"])
    raven_decorator = \
            raven.flask_glue.AuthDecorator(
                    require_principal=app.config["ADMIN_CRSIDS"])


## PostgreSQL

def connection():
    """
    Get a connection to use in this request

    If no connection has been used in this request, it connects to the
    database. Further calls to connection() in this request context will
    get the same connection.

    The connection is committed and closed at the end of the request.
    """

    assert flask.has_request_context()
    if not hasattr(g, '_database'):
        g._database = psycopg2.connect(app.config["POSTGRES"])
    return g._database

def cursor(real_dict_cursor=False):
    """
    Get a postgres cursor for immediate use during a request

    If a cursor has not yet been used in this request, it connects to the
    database. Further cursors re-use the per-request connection.

    The connection is committed and closed at the end of the request.

    If real_dict_cursor is set, a RealDictCursor is returned
    """

    if real_dict_cursor:
        f = RealDictCursor
        return connection().cursor(cursor_factory=f)
    else:
        return connection().cursor()

@app.teardown_appcontext
def close_db_connection(exception):
    """Commit and close the per-request postgres connection"""

    if hasattr(g, '_database'):
        try:
            g._database.commit()
        finally:
            g._database.close()


## Logging and call_log

logger = logging.getLogger("notam")
call_logger = logging.getLogger("notam.call")

def get_sid():
    """Get the active call SID from the request, using parent_sid if present"""

    assert flask.has_request_context()

    if "parent_sid" in request.args:
        # in call_human_pickup: the TwiML executes on the dialed party
        # before connecting to the call, and has a separate ID
        return request.args["parent_sid"]
    else:
        return request.form["CallSid"]

def call_log(message):
    """Log message (via logging) and add it to the call_log table"""

    assert flask.has_request_context()

    sid = get_sid()
    call_logger.info("%s %s", sid, message)

    db_msg = message.encode('ascii', 'replace')

    query1 = "SELECT id FROM calls WHERE sid = %s"
    query2 = "INSERT INTO calls (sid) VALUES (%s) RETURNING id"
    query3 = "INSERT INTO call_log (call, time, message) " \
             "VALUES (%s, LOCALTIMESTAMP, %s)"

    with cursor() as cur:
        cur.execute(query1, (sid, ))
        if not cur.rowcount:
            cur.execute(query2, (sid, ))
        call_id = cur.fetchone()[0]
        cur.execute(query3, (call_id, db_msg))

def get_call_sid(call_id):
    """Get the call SID for a call id"""

    query = "SELECT sid FROM calls WHERE id = %s"
    with cursor() as cur:
        cur.execute(query, (call_id, ))
        if cur.rowcount:
            return cur.fetchone()[0]
        else:
            raise ValueError("No such call_id")

def get_call_log_for_id(call_id, return_dicts=False):
    """
    Get the whole call log for a given call id

    If return_dicts is false, a list of (time, message) tuples is returned;
    if true {"time": time, "message": message} dicts.
    """

    query = "SELECT time, message FROM call_log " \
            "WHERE call = %s " \
            "ORDER BY time ASC, id ASC"

    with cursor(return_dicts) as cur:
        cur.execute(query, (call_id, ))
        return cur.fetchall()

def get_call_log_for_sid(sid=None, return_dicts=False):
    """
    Get the whole call log for a given call SID

    If no SID is specified, it will use the SID from the current request.

    If return_dicts is false, a list of (time, message) tuples is returned;
    if true {"time": time, "message": message} dicts.
    """

    if sid is None:
        sid = get_sid()

    query = "SELECT time, message FROM call_log " \
            "WHERE call = (SELECT id FROM calls WHERE sid = %s) " \
            "ORDER BY time ASC, id ASC"

    with cursor(return_dicts) as cur:
        cur.execute(query, (sid, ))
        return cur.fetchall()

def calls_count():
    """Count the rows in the calls table"""

    query = "SELECT COUNT(*) AS count FROM calls"

    with cursor() as cur:
        cur.execute(query)
        return cur.fetchone()[0]

def call_log_first_lines(offset=0, limit=100):
    """
    Get a list of calls and for each, their first lines in the call log

    A list of {"call": call_id, "first_time": time, "first_message": message}
    dicts is returned.
    """
    # assumes entries in the calls table have at least one line in the log

    query = "SELECT " \
            "DISTINCT ON (call) " \
            "   call, time AS first_time, " \
            "   message AS first_message " \
            "FROM call_log " \
            "ORDER BY call ASC, time ASC, id ASC " \
            "LIMIT %s OFFSET %s"

    with cursor(True) as cur:
        cur.execute(query, (limit, offset))
        return cur.fetchall()

def email(subject, message):
    """Send an email"""

    logger.debug("email: %s %r", subject, message)

    email = "From: {0}\r\nTo: {1}\r\nSubject: CUSF Notam Twilio {2}\r\n\r\n" \
        .format(app.config['EMAIL_FROM'], ",".join(app.config['EMAIL_TO']),
                subject) \
        + message

    server = smtplib.SMTP(app.config['EMAIL_SERVER'])
    server.sendmail(app.config['EMAIL_FROM'], app.config['EMAIL_TO'], email)
    server.quit()


## Other database queries

def all_humans():
    """
    Get all humans, sorted by priority then name

    A list of {"id": id, "name": name, "phone": phone, "priority": priority}
    dicts is returned.
    """

    query = "SELECT id, name, phone, priority FROM humans " \
            "ORDER BY priority ASC, name ASC " \

    # put disabled humans at the end. priority is a smallint, so...
    key = lambda h: 100000 if h["priority"] == 0 else h["priority"]

    with cursor(True) as cur:
        cur.execute(query)
        humans = cur.fetchall()
        humans.sort(key=key)
        return humans

def update_human_priority(human_id, new_priority):
    """Update the priority column of a single human"""
    query = "UPDATE humans SET priority = %s WHERE id = %s"
    with cursor() as cur:
        cur.execute(query, (new_priority, human_id))

def add_human(name, phone, priority):
    """Add a human"""
    query = "INSERT INTO humans (name, phone, priority) VALUES (%s, %s, %s)"
    with cursor() as cur:
        cur.execute(query, (name, phone, priority))

def shuffled_humans(seed):
    """
    Get all humans, sorted by priority.

    Humans with equal priorities are shuffled randomly, with an RNG
    seeded with seed.

    Returns a list of (priority, name, phone) tuples.
    """

    query = "SELECT priority, name, phone FROM humans " \
            "WHERE priority > 0 ORDER BY id ASC"

    with cursor() as cur:
        cur.execute(query)
        humans = cur.fetchall()

    rng = random.Random(seed)
    humans = [(priority + rng.uniform(0.1, 0.2), name, phone)
              for (priority, name, phone) in humans]
    humans.sort()

    return humans

_message_query = "SELECT m.id, m.active_when, m.short_name, " \
                 "       m.web_short_text, m.web_long_text, " \
                 "       m.call_text, m.forward_to, " \
                 "       h.name AS forward_name, h.phone AS forward_phone, " \
                 "       LOCALTIMESTAMP <@ active_when AS active " \
                 "FROM messages AS m " \
                 "LEFT OUTER JOIN humans AS h ON m.forward_to = h.id "

def active_message():
    """
    Get the active message, if it exists.

    Returns a {"id": i, "active_when": a, "short_name": s,
    "web_short_text": wst, "web_long_text": wlt, "call_text": ct,
    "forward_to": human_id, "forward_name": human_name,
    "forward_phone": human_phone, "active": bool} dict,
    or None if there isn't an active message.
    """

    query = _message_query + \
            "WHERE LOCALTIMESTAMP <@ active_when"

    with cursor(True) as cur:
        cur.execute(query)
        if cur.rowcount == 1:
            return cur.fetchone()
        elif cur.rowcount == 0:
            return None
        else:
            raise AssertionError("cur.rowcount should be 0 or 1")

def messages_count():
    """Count the rows in the messages table"""

    query = "SELECT COUNT(*) AS count FROM messages"

    with cursor() as cur:
        cur.execute(query)
        return cur.fetchone()[0]

def all_messages(offset, limit=5):
    """
    Get all messages

    Returns a list of messages in the same form as active_message()
    """

    query = _message_query + \
            "ORDER BY active_when " \
            "OFFSET %s LIMIT %s"


    with cursor(True) as cur:
        cur.execute(query, (offset, limit))
        return cur.fetchall()

def get_message(message_id):
    """
    Gets a specific message by its id

    Returns all columns, as a dict.
    """

    query = "SELECT * FROM messages WHERE id = %s"

    with cursor(True) as cur:
        cur.execute(query, (message_id, ))
        return cur.fetchone()

_message_columns = ("short_name", "web_short_text", "web_long_text",
                    "call_text", "forward_to", "active_when")

def upsert_message(message):
    """
    Insert or update a message (depending on whether message["id"] is present)

    message should be a dict in the same form as active_message() returns,
    minus the 'active', 'forward_name', 'forward_phone' keys,
    and with the 'id' key optional.

    Automatically moves intersecting (active_when) messages out of the way,
    returning a list of affected message dicts in form:
    {"id": id, "short_name": short_name, "action": action}
    where action is one of "deleted", "end_earlier", "start_later".
    """

    query1 = "WITH " \
             "deleted AS ( " \
             "    DELETE FROM messages " \
             "    WHERE {0} active_when <@ %(n)s " \
             "    RETURNING 'delete'::TEXT AS action, " \
             "        short_name, active_when "\
             "), " \
             "end_earlier AS ( " \
             "    UPDATE messages " \
             "    SET active_when = " \
             "        TSRANGE(LOWER(active_when), LOWER(%(n)s)) " \
             "    WHERE {0} NOT active_when <@ %(n)s AND " \
             "        active_when && %(n)s AND active_when < %(n)s " \
             "    RETURNING 'end_earlier'::TEXT AS action, " \
             "        short_name, active_when "\
             "), " \
             "start_later AS ( "\
             "    UPDATE messages " \
             "    SET active_when = " \
             "        TSRANGE(UPPER(%(n)s), UPPER(active_when)) " \
             "    WHERE {0} NOT active_when <@ %(n)s AND " \
             "        active_when && %(n)s AND active_when > %(n)s " \
             "    RETURNING 'start_later'::TEXT AS action, " \
             "        short_name, active_when "\
             ") " \
             "SELECT action, short_name, active_when FROM deleted " \
             "UNION SELECT action, short_name, active_when FROM end_earlier " \
             "UNION SELECT action, short_name, active_when FROM start_later " \
             "ORDER BY active_when"

    query1_existing = query1.format("id != %(id)s AND ")
    query1_new = query1.format("")

    query2 = "UPDATE messages SET {0} WHERE id = %(id)s" \
             .format(','.join('{0} = %({0})s'.format(c)
                     for c in _message_columns))
    # insert query is in insert_message()

    new = message.get("id", None) is None
    query1 = query1_new if new else query1_existing

    with cursor() as cur:
        params = {"n": message["active_when"], "id": message.get("id", None)}
        cur.execute(query1, params)

        moved_messages = [(action, short_name)
                for action, short_name, active_when in cur.fetchall()]
        if new:
            insert_message(message)
        else:
            cur.execute(query2, message)

    return moved_messages

def insert_message(message):
    """
    Inserts a message.

    Unlike upsert_message, doesn't move other messages out of the way,
    and won't update an existing message
    """

    query = "INSERT INTO messages ({0}) VALUES ({1})" \
             .format(','.join(_message_columns),
                     ','.join('%({0})s'.format(c)
                     for c in _message_columns))

    with cursor() as cur:
        cur.execute(query, message)

def do_delete_message(message_id):
    """Delete message by id"""
    query = "DELETE FROM messages WHERE id = %s"
    with cursor() as cur:
        cur.execute(query, (message_id, ))

def check_active_clear(active_when):
    """Check there are no messages intersecting with active_when"""

    query = "SELECT count(*) FROM messages WHERE active_when && %s"

    with cursor() as cur:
        cur.execute(query, (active_when, ))
        return cur.fetchone()[0] == 0


## Misc

basic_phone_re = re.compile('^\\+[0-9]+$')
parse_datetime = lambda s: datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

@app.template_global('csrf_token')
def csrf_token():
    """
    Return the CSRF token for the current session

    If not already generated, session["_csrf_token"] is set to a random
    string. The token is used for the whole life of the session.
    """

    if "_csrf_token" not in session:
        session["_csrf_token"] = hex(random.getrandbits(64))
    return session["_csrf_token"]

@app.template_global('csrf_token_input')
def csrf_token_input():
    """Returns a hidden input element with the CSRF token"""
    return Markup('<input type="hidden" name="_csrf_token" value="{0}">') \
            .format(csrf_token())

def check_csrf_token():
    """Checks that request.form["_csrf_token"] is correct, aborting if not"""
    if "_csrf_token" not in request.form:
        logger.warning("Expected CSRF Token: not present")
        abort(400)
    if request.form["_csrf_token"] != csrf_token():
        logger.warning("CSRF Token incorrect")
        abort(400)

def check_twilio_request():
    """Checks that a request actually came from Twilio"""
    if not twilio_validator.validate(request.url, request.form,
                                     request.headers["X-Twilio-Signature"]):
        logger.warning("Twilio signature incorrect")
        abort(400)

@app.before_request
def validate_request():
    """Check POST/form requests: CSRF, or Twilio RequestValidator"""
    if request.path.startswith("/admin/"):
        r = raven_decorator.before_request()
        if r is not None:
            return r

    if request.endpoint and (request.form or request.method == "POST"):
        if request.endpoint.startswith("twilio_"):
            assert request.path.startswith("/twilio/")
            check_twilio_request()
        else:
            assert request.path.startswith("/admin/")
            check_csrf_token()

@app.template_global('show_which_pages')
def show_which_pages(page, pages, show=5):
    """
    Work out which page numbers to display

    Pages are numbered 1 to pages, inclusive.

    page: the current page
    pages: the total number of pages.
    show: maximum number of pages to show

    show must be odd.
    """

    assert show % 2 == 1

    if pages < show:
        return range(1, pages + 1)
    elif page <= (show // 2):
        return range(1, show + 1)
    elif page > pages - (show // 2):
        return range(pages - show + 1, pages + 1)
    else:
        return range(page - (show // 2), page + (show // 2) + 1)

@app.template_global('show_which_pages_responsive')
def show_which_pages_responsive(page, pages, phone=5, tablet=7, desktop=11):
    """
    Run show_which_pages for multiple show sizes

    For each type (phone, tablet, desktop) calls
    show_which_pages(page, pages, type).
    It then takes the unions of all the page numbers returned, and builds
    (and returns) a list of (page_number, class) tuples,
    where class is 'hidden-tablet' or 'hidden-phone hidden-tablet' as
    appropriate.

    Need phone <= tablet <= desktop.
    """

    if not phone <= tablet <= desktop:
        raise ValueError("Need phone <= tablet <= desktop")

    all_pages = show_which_pages(page, pages, desktop)
    tablet_pages = set(show_which_pages(page, pages, tablet))
    phone_pages = set(show_which_pages(page, pages, phone))

    # phone_pages < tablet_pages < all_pages
    assert tablet_pages - set(all_pages) == set()
    assert phone_pages - tablet_pages == set()

    def page_class(page):
        if page in phone_pages:
            return ''
        elif page in tablet_pages:
            return 'hidden-phone'
        else:
            return 'hidden-phone hidden-tablet'

    return [(page, page_class(page)) for page in all_pages]

@app.template_global('datetime_now')
def datetime_now():
    """Return now(), for templates"""
    return datetime.datetime.now()

def default_active_when():
    """
    Return a default DateTimeRange to popualate new message row forms

    The lower bound is tomorrow at midnight, the upper is the midnight after.
    """

    today = datetime.datetime.now() \
            .replace(hour=0, minute=0, second=0, microsecond=0)
    days = lambda n: datetime.timedelta(days=n)
    lower = today + days(1)
    upper = today + days(2)
    return DateTimeRange(lower, upper, bounds='[)')

def default_launch_date():
    """Returns midday in four days time"""
    midday = datetime.datetime.now() \
             .replace(hour=12, minute=0, second=0, microsecond=0)
    return midday + datetime.timedelta(days=4)

def intbrq(value):
    """Return int(value) or abort(400) if it fails (by ValueError)"""
    try:
        return int(value)
    except:
        abort(400)

def parse_message_edit_form():
    """
    Parse message_edit.html's form from request.form into a message dict

    Tolerates:
     - active_when missing: ignored (the active_when key is ommitted)

    Parse failure actions:
     - forward_to integer parsing fails: 400 Bad Request (from intbrq)
     - datetime parse failure: Store active_when as a dict
       (rather than DateTimeRange) in form
       {"lower": string, "upper": string} for repopulating the form
       (attributes vs items is not an issue for jinja2);
       set message["active_when_invalid"] to true.

    It's up to the caller to check call_text xor forward_to,
    whether "active_when" is present, and whether active_when_invalid is
    set, as appropriate.
    """

    message = {}

    for key in ("short_name", "web_short_text", "web_long_text",
                "call_text", "forward_to"):
        message[key] = request.form[key]

    if message["call_text"] == "":
        message["call_text"] = None

    if message["forward_to"] == "":
        message["forward_to"] = None
    else:
        message["forward_to"] = intbrq(message["forward_to"])

    try:
        lower = parse_datetime(request.form["active_when_lower"])
        upper = parse_datetime(request.form["active_when_upper"])
        if lower >= upper:
            raise ValueError
    except KeyError:
        pass
    except ValueError:
        # Note that we could produce a KeyError here if _lower is present
        # but _upper is not. This is not an issue, since a client sending
        # only is clearly a bad client and deserves the HTTP 400 it will get.
        message["active_when"] = {"lower": request.form["active_when_lower"],
                                  "upper": request.form["active_when_upper"]}
        message["active_when_invalid"] = True
    else:
        message["active_when"] = DateTimeRange(lower, upper, bounds='[)')

    return message

def wizard_ranges(launch_date_unrounded):
    """
    Return the active_when ranges to be used by the wizard

    Returns (active_all, active_call_text, active_forward_to) where:

     - active_forward_text runs from 8am on launch day, or 3 hours before the
       launch (whichever is earlier), until 3 hours after the launch
     - active_call_text runs from midnight 3 days in advance (round down to
       midnight) until active_forward_text starts
     - active_all covers both

    Lower bounds are rounded down, and upper bounds up, to whole hours.
    """

    hours = lambda n: datetime.timedelta(hours=n)

    launch_date = launch_date_unrounded \
                  .replace(minute=0, second=0, microsecond=0)

    start_forward_to = min(launch_date - hours(3), launch_date.replace(hour=8))

    end_forward_to = launch_date + hours(3)
    if launch_date != launch_date_unrounded:
        end_forward_to += hours(1) # round up

    start_call_text = launch_date.replace(hour=0) - datetime.timedelta(days=3)

    return (DateTimeRange(start_call_text, end_forward_to, bounds='[)'),
            DateTimeRange(start_call_text, start_forward_to, bounds='[)'),
            DateTimeRange(start_forward_to, end_forward_to, bounds='[)'))

def wizard_checks(active_all):
    """
    Checks if it's safe to run the wizard.

    Checks:

     - If there are messages in the way of the wizard.
     - If the wizard would try to add messages in the past.

    Returns an error message, or None if everything is OK
    """

    if active_all.lower < datetime.datetime.now():
        return "Launch is too close: you'll have to add this manually"

    elif not check_active_clear(active_all):
        return "There are messages that intersect with the datetime ranges " \
                "the wizard would want to use: you'll have to add this " \
                "manually"

    else:
        return None

def wizard_default_text(launch_date):
    """Default text to populate the message edit form from a launch_date"""
    # http://xkcd.com/1205/ ?

    if launch_date.hour < 11:
        day_time = "Early on", "early on", "early"
    elif 11 <= launch_date.hour <= 14:
        day_time = "Midday", "around midday", "around midday on"
    elif 15 <= launch_date.hour <= 17:
        day_time = "Afternoon of", "in the afternoon of", "the afternoon of"
    else:
        day_time = "Late on", "late on", "late on"

    def ordinal(n):
        if 11 <= n % 100 <= 13: return "th"
        elif n % 10 == 1: return "st"
        elif n % 10 == 2: return "nd"
        elif n % 10 == 3: return "rd"
        else: return "th"

    day_name = launch_date.strftime("%A")
    ordinald = "{0}{1}".format(launch_date.day, ordinal(launch_date.day))
    date_name = launch_date.strftime("%A {0} %B").format(ordinald)

    message = {"web_short_text": "Launch: {0} {1} {2}"
                    .format(day_time[0], day_name, ordinald),
               "web_long_text": "There may be a balloon release, weather "
                    "depending, {0} {1}. There will be no launches until then."
                    .format(day_time[1], date_name),
               "call_text": "We are planning a launch for {0} {1}"
                    .format(day_time[2], date_name)}

    return message


## Views

@app.route("/")
def redirect_admin():
    return redirect(url_for("home"))

@app.route("/admin/")
def home():
    return render_template("home.html", message=active_message())

@app.route("/admin/log")
@app.route("/admin/log/<int:page>")
def log_viewer(page=None):
    page_size = 100
    count = calls_count()

    if count == 0:
        if page is not None:
            abort(404)
        else:
            return render_template("log_viewer_empty.html")

    pages = count / page_size
    if count % page_size:
        pages += 1

    if page is None:
        return redirect(url_for(request.endpoint, page=pages))

    if page > pages or page < 1:
        abort(404)

    offset = (page - 1) * page_size
    calls = call_log_first_lines(offset, page_size)

    return render_template("log_viewer.html",
                calls=calls, pages=pages, page=page)

@app.route("/admin/log/call/<int:call>")
def log_viewer_call(call):
    try:
        sid = get_call_sid(call)
    except ValueError:
        abort(404)

    log = get_call_log_for_id(call, return_dicts=True)
    if not log:
        abort(404)

    return render_template("log_viewer_call.html", call=call, sid=sid, log=log,
                           return_to=request.args.get("return_to", None))

@app.route("/admin/humans", methods=["GET", "POST"])
def edit_humans():
    # if the update succeeds, redirect so that the method becomes GET and
    # the refresh button works as espected.
    # flask handles the message flashing and the template fills out the form
    # with either current or failed-to-update values.

    # note that the update/insert queries will have been the first in this
    # request's transaction, so the rollback doesn't hit anything unexpected

    if request.form.get("edit_priorities", False):
        changed = 0

        try:
            for human in all_humans():
                field_name = "priority_{0}".format(human["id"])
                new_priority = intbrq(request.form[field_name])
                if human["priority"] != new_priority:
                    update_human_priority(human["id"], new_priority)
                    changed += 1

        except psycopg2.DataError:
            connection().rollback()
            logger.warning("PostgreSQL error", exc_info=True)
            abort(400)

        else:
            if changed:
                if changed == 1:
                    flash('Priority updated', 'success')
                else:
                    flash('{0} priorities updated'.format(changed), 'success')
            else:
                flash('No priorioties changed', 'warning')

            return redirect(url_for(request.endpoint))

    elif request.form.get("add_human", False):
        name = request.form["name"]
        phone = request.form["phone"]
        priority = intbrq(request.form["priority"])

        try:
            add_human(name, phone, priority)

        except psycopg2.IntegrityError as e:
            connection().rollback()
            if e.pgcode == psycopg2.errorcodes.UNIQUE_VIOLATION:
                flash('Name and phone must be unique', 'error')
            else:
                logger.warning("PostgreSQL error", exc_info=True)
                abort(400)

        except psycopg2.DataError:
            connection().rollback()
            logger.warning("PostgreSQL error", exc_info=True)
            abort(400)

        else:
            flash('Human added', 'success')
            return redirect(url_for(request.endpoint))

    humans = all_humans()

    priorities = set(h["priority"] for h in humans)

    try:
        priorities.remove(0)
    except KeyError:
        pass

    lowest_priorities = sorted(priorities)[:2]
    while len(lowest_priorities) < 2:
        lowest_priorities.append(None)

    return render_template("humans.html",
            humans=humans,
            lowest_priorities=lowest_priorities)

@app.route("/admin/messages")
@app.route("/admin/messages/<int:page>")
def list_messages(page=None):
    page_size = 5
    count = messages_count()

    if count == 0:
        if page is not None:
            abort(404)
        else:
            return render_template("message_list.html",
                    default_launch_date=default_launch_date())

    pages = count / page_size
    if count % page_size:
        pages += 1

    if page is None:
        return redirect(url_for(request.endpoint, page=pages))

    if page > pages or page < 1:
        abort(404)

    offset = (page - 1) * page_size

    if offset == 0:
        messages = all_messages(offset, page_size)
    else:
        # so we can calculate gap_preceeding. It's dropped later
        messages = all_messages(offset - 1, page_size + 1)

    last_upper = None
    for message in messages:
        if last_upper is None: # first message
            message["gap_preceeding"] = not message["active"]
        else:
            message["gap_preceeding"] = \
                    last_upper != message["active_when"].lower

        last_upper = message["active_when"].upper

    if offset != 0:
        messages = messages[1:]

    return render_template("message_list.html", messages=messages,
            page=page, pages=pages, default_launch_date=default_launch_date())

@app.route("/admin/messages/new", methods=["GET"])
@app.route("/admin/message/<int:message_id>/edit", methods=["GET"])
def edit_message(message_id=None):
    if message_id is None:
        # tomorrow 00:00:00
        message = {"id": None, "active_when": default_active_when()}
    else:
        message = get_message(message_id)
        if message is None:
            abort(404)

    return render_template("message_edit.html", humans=all_humans(), **message)

@app.route("/admin/messages/new", methods=["POST"])
@app.route("/admin/message/<int:message_id>/edit", methods=["POST"])
def edit_message_save(message_id=None):
    if message_id is not None:
        new_type = "edited"
        action_name = "updated"
    else:
        new_type = "new"
        action_name = "added"

    message = parse_message_edit_form()
    if "active_when" not in message:
        abort(400)
    message["id"] = message_id

    if message.get("active_when_invalid", False):
        flash('Invalid datetime range', 'error')
        return render_template("message_edit.html",
                               humans=all_humans(), **message)

    if (message["call_text"] == None) == (message["forward_to"] == None):
        flash('Specify exactly one of "Twilio call text" and '
              '"immediately forward call to"', 'error')
        return render_template("message_edit.html",
                               humans=all_humans(), **message)

    try:
        moved_messages = upsert_message(message)
    except (psycopg2.IntegrityError, psycopg2.DataError):
        connection().rollback()
        logger.warning("PostgreSQL error", exc_info=True)
        abort(400)
    except psycopg2.InternalError as e:
        connection().rollback()
        if e.pgcode != psycopg2.errorcodes.RAISE_EXCEPTION:
            raise

        logger.warning("Forbidden update", exc_info=True)
        flash('Update forbidden: {0}'.format(e.diag.message_primary), 'error')

        if message_id is not None:
            # Trigger failures for updating existing messages are due to
            # forbidding all changes except upper bounds in some cases, so
            # reset the form:
            message = get_message(message_id)

        return render_template("message_edit.html",
                               humans=all_humans(), **message)

    for action, name in moved_messages:
        if action == "delete":
            flash('Deleted message "{0}" since {1} message '
                  'completely covers it.'.format(name, new_type),
                  'warning')
        elif action == "end_earlier":
            flash('Message "{0}" now ends earlier ({1}) since '
                  '{2} message starts then.'
                  .format(name, message["active_when"].lower, new_type),
                  'warning')
        elif action == "start_later":
            flash('Message "{0}" now starts later ({1}) since '
                  '{2} message starts then.'
                  .format(name, message["active_when"].upper, new_type),
                  'warning')

    flash("Message {0}".format(action_name), "success")
    return redirect(url_for("list_messages"))

@app.route("/admin/messages/wizard/start", methods=["POST"])
def wizard_start():
    try:
        launch_date = parse_datetime(request.form["launch_date"])
    except ValueError:
        flash('Invalid datetime', 'error')
        return redirect(url_for('list_messages'))

    active_all, active_call_text, active_forward_to = \
            wizard_ranges(launch_date)

    error = wizard_checks(active_all)
    if error:
        flash(error, 'error')
        return redirect(url_for('list_messages'))

    message = wizard_default_text(launch_date)
    message["id"] = None

    return render_template("message_edit.html", wizard_mode=True,
                           active_call_text=active_call_text,
                           active_forward_to=active_forward_to,
                           launch_date=launch_date,
                           humans=all_humans(), **message)


@app.route("/admin/messages/wizard/save", methods=["POST"])
def wizard_save():
    try:
        launch_date = parse_datetime(request.form["launch_date"])
    except ValueError:
        abort(400)

    active_all, active_call_text, active_forward_to = \
            wizard_ranges(launch_date)

    error = wizard_checks(active_all)
    if error:
        flash(error, 'error')
        return redirect(url_for('list_messages'))

    message1 = parse_message_edit_form()
    message1["id"] = None
    message2 = message1.copy()

    message1["forward_to"] = None
    message1["active_when"] = active_call_text
    message2["call_text"] = None
    message2["active_when"] = active_forward_to

    try:
        insert_message(message1)
        insert_message(message2)
    except (psycopg2.IntegrityError, psycopg2.DataError):
        connection().rollback()
        logger.warning("PostgreSQL error", exc_info=True)
        abort(400)
    except psycopg2.InternalError as e:
        connection().rollback()
        if e.pgcode != psycopg2.errorcodes.RAISE_EXCEPTION:
            raise
        logger.warning("Forbidden update", exc_info=True)
        abort(400)
    else:
        flash('Messages added successfully', 'success')
        return redirect(url_for('list_messages'))

@app.route("/admin/message/<int:message>/delete", methods=["POST"])
def delete_message(message):
    check_csrf_token() # since request.form would otherwise be empty

    try:
        do_delete_message(message)
    except psycopg2.InternalError as e:
        connection().rollback()
        if e.pgcode != psycopg2.errorcodes.RAISE_EXCEPTION:
            raise

        logger.warning("Forbidden delete", exc_info=True)
        flash('Delete forbidden: {0}'.format(e.diag.message_primary), 'error')
    else:
        flash("Message deleted", "success")

    return redirect(url_for('list_messages'))


## Views for other programs

@app.route("/heartbeat")
def heartbeat():
    with cursor() as cur:
        cur.execute("SELECT TRUE")
        assert cur.fetchone()
    return "uWSGI is alive and PostgreSQL is OK"

@app.route('/web.json')
def web_status():
    message = active_message()
    if not message:
        m = "No upcoming launches in the next three days"
        return jsonify(short=m, long=m)
    else:
        return jsonify(short=message["web_short_text"],
                       long=message["web_long_text"])


## Twilio URLS

@app.route('/twilio/sms', methods=["POST"])
def twilio_sms():
    sms_from = request.form["From"]
    sms_msg = request.form["Body"]
    logger.info("SMS From %s: %r", sms_from, sms_msg)

    r = twiml.Response()
    return str(r)

@app.route('/twilio/call/start', methods=["POST"])
def twilio_call_start():
    call_log("Call started; from {0}".format(request.form["From"]))

    message = active_message()
    r = twiml.Response()

    if message and message["forward_to"]:
        name = message["forward_name"]
        phone = message["forward_phone"]

        call_log("Forwarding call straight to {0!r} on {1}"
            .format(name, phone))

        pickup_url = url_for("twilio_call_forward_pickup",
                             parent_sid=get_sid())
        d = r.dial(action=url_for("twilio_call_forward_ended"),
                   callerId=request.form["To"])
        d.number(phone, url=url_for("twilio_call_forward_pickup"))

    else:
        # This is the information phone number for the Cambridge University
        # Spaceflight NOTAM.
        r.play(url_for('static', filename='audio/greeting.wav'))
        r.pause(length=1)

        if not message:
            call_log("Saying 'no launches in the next three days' "
                     "and offering options")
            # We are not planning any launches in the next three days.
            r.play(url_for('static', filename='audio/none_three_days.wav'))
        else:
            call_text = message["call_text"]
            call_log("Introducing robot and saying {0!r}".format(call_text))
            # You will shortly hear an automated message detailing the
            # approximate time of an upcoming launch that we are planning.
            r.play(url_for('static', filename='audio/robot_intro.wav'))
            r.pause(length=1)
            r.say(call_text)

        r.pause(length=1)
        twilio_options(r)

    return str(r)

def twilio_options(r):
    g = r.gather(action=url_for("twilio_call_gathered"),
                 timeout=30, numDigits=1)
    # Hopefully this automated message has answered your question, but if not,
    # please press 2 to be forwarded to a human. Otherwise, either hang up or
    # press 1 to end the call.
    g.play(url_for('static', filename='audio/options.wav'))
    r.redirect(url_for('twilio_call_gather_failed'))

@app.route('/twilio/call/gathered', methods=["POST"])
def twilio_call_gathered():
    d = request.form["Digits"]
    r = twiml.Response()

    if d == "1":
        call_log("Hanging up (pressed 1)")

    elif d == "2":
        seed = random.getrandbits(32)
        call_log("Trying humans (pressed 2); seed {0!r}".format(seed))
        # Forwarding. In the event that the first society member contacted is
        # in a lecture or otherwise unavailable, a second member will be
        # phoned. This could take a minute or two.
        r.play(url_for('static', filename='audio/forwarding.wav'))
        r.pause(length=1)
        # call_human(seed, 0)
        twilio_dial(r, seed, 0)

    else:
        call_log("Invalid keypress {0}; offering options".format(d))
        twilio_options(r)

    return str(r)

@app.route('/twilio/call/gather_failed', methods=["POST"])
def twilio_call_gather_failed():
    call_log("Gather failed - no keys pressed; hanging up")
    r = twiml.Response()
    r.hangup()
    return str(r)

def twilio_dial(r, seed, index):
    priority, name, phone = shuffled_humans(seed)[index]

    call_log("Attempt {0}: {1!r} on {2}".format(index, name, phone))

    # Make callerId be our Twilio number so people know why they're being
    # called at 7am before they pick up
    pickup_url = url_for("twilio_call_human_pickup", seed=seed, index=index,
                         parent_sid=get_sid())
    d = r.dial(action=url_for("twilio_call_human_ended",
                              seed=seed, index=index),
               callerId=request.form["To"])
    d.number(phone, url=pickup_url)

@app.route('/twilio/call/human/<int:seed>/<int:index>', methods=["POST"])
def twilio_call_human(seed, index):
    r = twiml.Response()
    twilio_dial(r, seed, index)
    return str(r)

@app.route("/twilio/call/human/<int:seed>/<int:index>/pickup",
           methods=["POST"])
def twilio_call_human_pickup(seed, index):
    # This URL is hit before the called party is connected to the call
    # Just use it for logging
    call_log("Human (attempt {0}) picked up".format(index))
    r = twiml.Response()
    return str(r)

@app.route("/twilio/call/human/<int:seed>/<int:index>/end", methods=["POST"])
def twilio_call_human_ended(seed, index):
    # This URL is hit when the Dial verb finishes

    status = request.form["DialCallStatus"]
    r = twiml.Response()

    if status == "completed":
        call_log("Dial (attempt {0}) completed successfully; hanging up"
                    .format(index))
        r.hangup()

    else:
        call_log("Dialing human (attempt {0}) failed: {1}"
                    .format(index, status))

        try:
            twilio_dial(r, seed, index + 1)
        except IndexError:
            call_log("Humans exhausted: apologising and hanging up")
            # Unfortunately we failed to contact any members.
            # Please try the alternative phone number on the NOTAM
            r.play(url_for('static', filename='audio/humans_fail.wav'))
            r.pause(length=1)
            r.hangup()

    return str(r)

@app.route("/twilio/call/forward/pickup", methods=["POST"])
def twilio_call_forward_pickup():
    call_log("Forwarded call picked up")
    r = twiml.Response()
    return str(r)

@app.route("/twilio/call/forward/ended", methods=["POST"])
def twilio_call_forward_ended():
    status = request.form["DialCallStatus"]
    if status == "completed":
        call_log("Forwarded call completed successfully. Hanging up.")
    else:
        call_log("Forwarded call failed: {0}. Hanging up.".format(status))

    r = twiml.Response()
    r.hangup()
    return str(r)

@app.route("/twilio/call/status_callback", methods=["POST"])
def twilio_call_ended():
    number = request.form["From"]
    duration = request.form["CallDuration"]
    status = request.form["CallStatus"]

    # Check that this is sane, it's going in the Subject header
    assert basic_phone_re.match(number)

    call_log("Call from {0} ended after {1} seconds with status '{2}'"
                .format(number, duration, status))

    fmt = lambda time, message: \
            "{0} {1}".format(time.strftime("%H:%M:%S"), message)
    lines = (fmt(time, message) for time, message in get_call_log_for_sid())
    call_log_str = "\n".join(lines)
    email("Call from {0}".format(number), call_log_str)

    return "OK"
