import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from billiard_app import create_app
from billiard_app.db import get_db
from billiard_app.maintenance import backup_warning, create_daily_backups, create_recent_backups
from billiard_app.money import MAX_MONEY_CENTS, to_cents, to_whole_cents
from billiard_app.services.business_day import current_business_day


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": str(Path(self.directory.name) / "test.db"), "SECRET_KEY": "test"})
        self.client = self.app.test_client()
        self.client.post('/settings/admin', data={'password': 'password-old', 'password_confirmation': 'password-old'})

    def tearDown(self):
        self.directory.cleanup()

    def value(self, sql, args=()):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchone()[0]

    def execute(self, sql, args=()):
        with self.app.app_context():
            db = get_db()
            result = db.execute(sql, args)
            db.commit()
            return result.lastrowid

    def form_tokens(self, path, action):
        html = self.client.get(path).get_data(as_text=True)
        form = re.search(r'<form\b[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', html, re.S)
        self.assertIsNotNone(form, action)
        return dict(re.findall(r'name="(_(?:csrf|operation)_token)" value="([^"]+)"', form.group(1)))

    def reserve(self, **changes):
        data = dict(table_no=1, guest_name='Guest', date='2026-09-22', start_time='19:00', end_time='')
        data.update(changes)
        return self.client.post('/reservations', data=data)

    def test_money_bound_and_extreme_exponents(self):
        self.assertEqual(to_cents('2147483647'), MAX_MONEY_CENTS)
        self.assertEqual(to_cents('12.345'), 1235)
        for amount in ('2147483647.01', '1e30', '1e1000000', '-1e1000000', 'nan', 'inf'):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    to_cents(amount)
                response = self.client.post('/finance/cash', data={'record_date': '2026-09-22', 'actual_revenue': amount})
                self.assertEqual(response.status_code, 302)
        self.assertEqual(self.value('SELECT count(*) FROM daily_cash_records'), 0)

    def test_split_package_uses_real_payments_and_labels_list_prices(self):
        self.client.post('/settings/rates', data={
            'table_count': '10', 'package_rate_1': '7.5', 'package_enabled_1': '1',
        })
        self.client.post('/sessions/start', data={
            'table_no': '1', 'mode': 'package', 'package_hours': '1',
        })
        session_id = self.value('SELECT id FROM sessions WHERE table_no=1 AND status="active"')
        self.client.post(f'/sessions/extend/{session_id}', data={'extra_hours': '1'})
        self.assertEqual(self.value('SELECT SUM(amount_cents) FROM payments'), 1600)
        detail = self.client.get('/tables/1').get_data(as_text=True)
        self.assertIn('球檯費原價：15 元', detail)
        self.assertIn('已收款：<strong>16 元</strong>', detail)
        self.assertIn('結束包台時待收：<strong>0 元</strong>', detail)
        self.assertNotIn('球檯費（折扣後）', detail)
        stats = self.client.get(f'/stats?date={current_business_day().isoformat()}').get_data(as_text=True)
        self.assertRegex(stats, r'計價原價總額</h3>\s*<p class="big-number">15</p>')
        self.assertRegex(stats, r'實際已收</h3>\s*<p class="big-number">16</p>')

    def test_cash_and_expenses_require_whole_yuan(self):
        self.assertEqual(to_whole_cents('7.00'), 700)
        for amount in ('7.5', '1.001', '0.001'):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    to_whole_cents(amount)
                self.client.post('/finance/cash', data={
                    'record_date': '2026-09-22', 'actual_revenue': amount,
                })
                self.client.post('/finance/expenses', data={
                    'expense_date': '2026-09-22', 'category': '其他', 'amount': amount,
                })
        self.assertEqual(self.value('SELECT COUNT(*) FROM daily_cash_records'), 0)
        self.assertEqual(self.value('SELECT COUNT(*) FROM expenses'), 0)
        page = self.client.get('/finance').get_data(as_text=True)
        self.assertIn('name="actual_revenue" min="0" step="1"', page)
        self.assertIn('name="amount" min="1" step="1"', page)

    def test_backup_health_checks_missing_stale_corrupt_and_secondary_snapshots(self):
        database = self.app.config['DATABASE']
        create_daily_backups(database)
        self.assertIn('快照尚未建立', backup_warning(database))
        recent, _ = create_recent_backups(database)
        self.assertIsNone(backup_warning(database))
        stale = (datetime.now() - timedelta(minutes=31)).timestamp()
        os.utime(recent, (stale, stale))
        self.assertIn('超過 30 分鐘', backup_warning(database))
        os.utime(recent, None)
        recent.write_bytes(b'not a sqlite database')
        self.assertIn('快照需要檢查', backup_warning(database))
        recent.unlink()
        create_recent_backups(database)
        secondary = Path(self.directory.name) / 'secondary'
        create_daily_backups(database, str(secondary))
        self.assertIn('快照尚未建立', backup_warning(database, str(secondary)))
        create_recent_backups(database, str(secondary))
        self.assertIsNone(backup_warning(database, str(secondary)))

    def test_audit_log_records_high_risk_actions_without_free_text(self):
        self.client.post('/finance/cash', data={
            'record_date': '2026-09-22', 'actual_revenue': '100', 'note': 'private cash note',
        })
        self.client.post('/finance/cash', data={
            'record_date': '2026-09-22', 'actual_revenue': '120', 'note': 'new private note',
        })
        self.client.post('/finance/expenses', data={
            'expense_date': '2026-09-22', 'category': '其他', 'amount': '50',
            'description': 'private expense note',
        })
        expense_id = self.value('SELECT id FROM expenses')
        self.client.post(f'/finance/expenses/{expense_id}/delete')
        item = self.value("SELECT m.id FROM menu_items m JOIN categories c ON c.id=m.category_id WHERE c.name!='飲料' LIMIT 1")
        self.client.post('/orders/add', data={'table_no': '1', 'item_id': str(item), 'quantity': '1'})
        order_id = self.value('SELECT id FROM orders')
        self.client.post(f'/orders/{order_id}/delete')
        self.assertEqual(self.value('SELECT COUNT(*) FROM audit_events'), 4)
        with self.app.app_context():
            rows = get_db().execute('SELECT action, before_value, after_value FROM audit_events ORDER BY id').fetchall()
        self.assertEqual([row['action'] for row in rows], [
            'cash_update', 'cash_update', 'expense_delete', 'order_delete',
        ])
        self.assertEqual(rows[1]['before_value'], '100 元')
        self.assertEqual(rows[1]['after_value'], '120 元；備註已修改')
        self.assertIn('50 元', rows[2]['before_value'])
        self.assertIn('金額', rows[3]['before_value'])
        values = ' '.join(row['before_value'] + row['after_value'] for row in rows)
        self.assertNotIn('private', values)
        page = self.client.get('/settings/audit').get_data(as_text=True)
        self.assertIn('刪除訂單', page)
        self.assertIn('修改每日實收', page)
        self.assertEqual(self.app.test_client().get('/settings/audit').status_code, 302)

    def test_audit_failure_rolls_back_cash_update(self):
        self.execute("""CREATE TRIGGER reject_audit BEFORE INSERT ON audit_events
                       BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.post('/finance/cash', data={
                'record_date': '2026-09-22', 'actual_revenue': '100',
            })
        self.assertEqual(self.value('SELECT COUNT(*) FROM daily_cash_records'), 0)

    def test_reset_keeps_an_audit_event(self):
        self.client.post('/finance/cash', data={
            'record_date': '2026-09-22', 'actual_revenue': '100',
        })
        self.client.post('/settings/reset-test-data', data={
            'confirm_scope': 'yes', 'confirmation': '永久刪除測試營運資料',
            'confirmation_date': date.today().isoformat(),
        })
        self.assertEqual(self.value('SELECT COUNT(*) FROM daily_cash_records'), 0)
        self.assertEqual(self.value("SELECT COUNT(*) FROM audit_events WHERE action='data_reset'"), 1)

    def test_package_limit_includes_extensions_and_rejects_overflow(self):
        for hours in ('17', '999999999999999999999999999', '0'):
            self.client.post('/sessions/start', data={'table_no': 3, 'mode': 'package', 'package_hours': hours})
        self.assertEqual(self.value('SELECT count(*) FROM sessions'), 0)
        self.client.post('/sessions/start', data={'table_no': 3, 'mode': 'package', 'package_hours': 15})
        sid = self.value('SELECT id FROM sessions')
        self.client.post(f'/sessions/extend/{sid}', data={'extra_hours': 2})
        self.assertEqual(self.value('SELECT package_hours FROM sessions'), 15)
        self.client.post(f'/sessions/extend/{sid}', data={'extra_hours': 1})
        self.client.post(f'/sessions/extend/{sid}', data={'extra_hours': 1})
        self.assertEqual(self.value('SELECT package_hours FROM sessions'), 16)
        self.assertEqual(self.value('SELECT count(*) FROM payments'), 2)

    def test_legacy_large_hours_do_not_break_dashboard(self):
        self.client.post('/sessions/start', data={'table_no': 3, 'mode': 'package', 'package_hours': 1})
        self.execute('UPDATE sessions SET package_hours = ?', (10**12,))
        for path in ('/', '/tables/3'):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_open_end_expires_after_one_hour_and_can_complete(self):
        self.reserve()
        self.reserve(start_time='19:30', end_time='19:45')
        self.assertEqual(self.value('SELECT count(*) FROM reservations'), 1)
        self.reserve(start_time='20:00', end_time='21:00')
        self.reserve(date='2026-10-22', start_time='19:00', end_time='20:00')
        self.assertEqual(self.value('SELECT count(*) FROM reservations'), 3)
        rid = self.value('SELECT min(id) FROM reservations')
        self.client.post(f'/reservations/{rid}/complete', data={'date': '2026-09-22'})
        self.reserve(start_time='19:00', end_time='20:00')
        self.assertEqual(self.value('SELECT count(*) FROM reservations'), 4)
        self.assertEqual(self.value('SELECT status FROM reservations WHERE id=?', (rid,)), 'completed')

    def test_early_morning_open_end_and_overnight_reservation(self):
        self.reserve(start_time='01:00')
        self.reserve(start_time='06:00', end_time='07:00')
        self.reserve(date='2026-09-30', start_time='23:00', end_time='01:00', ends_next_day='1', guest_name='Overnight')
        self.assertEqual(self.value('SELECT count(*) FROM reservations'), 3)
        self.reserve(date='2026-10-01', start_time='00:30', end_time='01:00')
        self.assertEqual(self.value('SELECT count(*) FROM reservations'), 3)
        page = self.client.get('/calendar?month=2026-10&date=2026-10-01').get_data(as_text=True)
        self.assertIn('Overnight', page)
        self.assertIn('2026-10-01', page)

    def test_password_change_revokes_other_browser_and_idle_timeout(self):
        other = self.app.test_client()
        other.post('/settings/admin', data={'password': 'password-old'})
        self.assertEqual(other.get('/settings').status_code, 200)
        self.client.post('/settings/admin/password', data={'current_password': 'password-old', 'new_password': 'password-new', 'new_password_confirmation': 'password-new'})
        self.assertEqual(self.client.get('/settings').status_code, 200)
        self.assertEqual(other.get('/settings').status_code, 302)
        response = other.post('/finance/cash', data={'record_date': '2026-09-22', 'actual_revenue': '12'})
        self.assertIn('/settings/admin', response.location)
        self.assertEqual(self.value('SELECT count(*) FROM daily_cash_records'), 0)
        with self.client.session_transaction() as session:
            session['admin_last_seen'] = time.time() - 1801
        self.assertEqual(self.client.get('/settings').status_code, 302)

    def test_disabled_employee_remains_selected_and_cannot_be_assigned_to_new_shift(self):
        employee = self.execute("INSERT INTO employees (name) VALUES ('Alice')")
        self.execute("INSERT INTO employees (name) VALUES ('Bob')")
        shift_type = self.value('SELECT min(id) FROM shift_types')
        data = {'employee_id': employee, 'shift_type_id': shift_type, 'dates': '2026-09-22', 'start_time': '10:00', 'end_time': '18:00'}
        self.client.post('/shifts/add', data=data)
        sid = self.value('SELECT id FROM shifts')
        self.execute('UPDATE employees SET is_active=0 WHERE id=?', (employee,))
        page = self.client.get('/shifts?month=2026-09&date=2026-09-22').get_data(as_text=True)
        self.assertIn(f'value="{employee}" selected>Alice（已停用）', page)
        self.client.post(f'/shifts/{sid}/update', data=dict(data, date='2026-09-22', note='Changed'))
        self.assertEqual(self.value('SELECT staff_name FROM shifts'), 'Alice')
        self.assertEqual(self.value('SELECT note FROM shifts'), 'Changed')
        self.client.post('/shifts/add', data=dict(data, dates='2026-09-23'))
        self.assertEqual(self.value('SELECT count(*) FROM shifts'), 1)

    def test_invalid_dates_and_csrf_fail_without_500(self):
        for value in ('0001-01-01', '9999-12-31'):
            for route in ('calendar', 'shifts', 'finance', 'stats'):
                with self.subTest(route=route, value=value):
                    self.assertEqual(self.client.get(f'/{route}?month={value[:7]}&date={value}').status_code, 200)
            self.reserve(date=value)
        self.assertEqual(self.value('SELECT count(*) FROM reservations'), 0)
        self.app.config['CSRF_ENABLED'] = True
        self.client.get('/')
        with self.client.session_transaction() as session:
            csrf = session['_csrf_token']
        for data, headers in (({'_csrf_token': '中文'}, {}), ({'_csrf_token': csrf}, {'Origin': 'http://['})):
            self.assertEqual(self.client.post('/waiting/create', data=data, headers=headers).status_code, 403)

    def test_duplicate_extension_is_atomic_and_changed_payload_rejected(self):
        self.client.post('/sessions/start', data={'table_no': 3, 'mode': 'package', 'package_hours': 1})
        sid = self.value('SELECT id FROM sessions')
        self.app.config['CSRF_ENABLED'] = True
        action = f'/sessions/extend/{sid}'
        data = dict(self.form_tokens('/tables/3', action), extra_hours='1')
        for _ in range(2):
            self.assertEqual(self.client.post(action, data=data).status_code, 302)
        self.assertEqual(self.value('SELECT package_hours FROM sessions'), 2)
        self.assertEqual(self.value('SELECT count(*) FROM payments'), 2)
        self.assertEqual(self.client.post(action, data=dict(data, extra_hours='2')).status_code, 409)
        self.assertEqual(self.value('SELECT count(*) FROM operation_receipts'), 1)

    def test_operation_token_is_required_and_tampering_is_rejected(self):
        self.app.config['CSRF_ENABLED'] = True
        data = dict(self.form_tokens('/tables/3', '/sessions/start'), table_no='3', mode='package', package_hours='1')
        token = data.pop('_operation_token')
        self.assertEqual(self.client.post('/sessions/start', data=data).status_code, 409)
        data['_operation_token'] = token + 'invalid'
        self.assertEqual(self.client.post('/sessions/start', data=data).status_code, 409)
        self.assertEqual(self.value('SELECT count(*) FROM sessions'), 0)

    def test_duplicate_order_and_failed_operation_retry(self):
        item = self.value("SELECT m.id FROM menu_items m JOIN categories c ON c.id=m.category_id WHERE c.name!='飲料' LIMIT 1")
        self.app.config['CSRF_ENABLED'] = True
        data = dict(self.form_tokens('/tables/1', '/orders/add'), table_no='1', item_id=str(item), quantity='0')
        self.client.post('/orders/add', data=data)
        self.assertEqual(self.value('SELECT count(*) FROM operation_receipts'), 0)
        data['quantity'] = '1'
        self.client.post('/orders/add', data=data)
        self.client.post('/orders/add', data=data)
        self.assertEqual(self.value('SELECT count(*) FROM orders'), 1)
        self.assertEqual(self.value('SELECT count(*) FROM operation_receipts'), 1)

    def test_duplicate_expense_is_not_recorded_twice(self):
        self.app.config['CSRF_ENABLED'] = True
        data = dict(
            self.form_tokens('/finance', '/finance/expenses'),
            expense_date='2026-09-22', category='其他', amount='50', description='Supplies',
        )
        for _ in range(2):
            self.assertEqual(self.client.post('/finance/expenses', data=data).status_code, 302)
        self.assertEqual(self.value('SELECT count(*) FROM expenses'), 1)
        self.assertEqual(self.value('SELECT count(*) FROM operation_receipts'), 1)
        self.assertEqual(self.client.post('/finance/expenses', data=dict(data, amount='60')).status_code, 409)

    def test_concurrent_replay_only_adds_one_extension(self):
        self.client.post('/sessions/start', data={'table_no': 3, 'mode': 'package', 'package_hours': 1})
        sid = self.value('SELECT id FROM sessions')
        self.app.config['CSRF_ENABLED'] = True
        action = f'/sessions/extend/{sid}'
        data = dict(self.form_tokens('/tables/3', action), extra_hours='1')
        other = self.app.test_client()
        other.set_cookie('session', self.client.get_cookie('session').value)
        barrier = threading.Barrier(3)
        errors = []
        responses = []
        def submit(client):
            try:
                barrier.wait()
                responses.append(client.post(action, data=data).status_code)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=submit, args=(client,)) for client in (self.client, other)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(responses, [302, 302])
        self.assertEqual(self.value('SELECT package_hours FROM sessions'), 2)
        self.assertEqual(self.value('SELECT count(*) FROM payments'), 2)

    def test_receipt_failure_rolls_back_order_and_retry_can_succeed(self):
        item = self.value("SELECT m.id FROM menu_items m JOIN categories c ON c.id=m.category_id WHERE c.name!='飲料' LIMIT 1")
        self.app.config['CSRF_ENABLED'] = True
        data = dict(self.form_tokens('/tables/1', '/orders/add'), table_no='1', item_id=str(item), quantity='1')
        self.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON operation_receipts BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.post('/orders/add', data=data)
        self.assertEqual(self.value('SELECT count(*) FROM orders'), 0)
        self.assertEqual(self.value('SELECT count(*) FROM customer_tabs'), 0)
        self.assertEqual(self.value('SELECT count(*) FROM operation_receipts'), 0)
        self.execute('DROP TRIGGER fail_receipt')
        self.assertEqual(self.client.post('/orders/add', data=data).status_code, 302)
        self.assertEqual(self.value('SELECT count(*) FROM orders'), 1)

    def test_derived_money_overflow_leaves_no_partial_data(self):
        self.execute('UPDATE table_rates SET package_rate_per_hour_cents=? WHERE table_no=3', (MAX_MONEY_CENTS,))
        self.assertEqual(self.client.post('/sessions/start', data={'table_no': 3, 'mode': 'package', 'package_hours': 2}).status_code, 302)
        self.assertEqual(self.value('SELECT count(*) FROM sessions'), 0)
        item = self.value("SELECT m.id FROM menu_items m JOIN categories c ON c.id=m.category_id WHERE c.name!='飲料' LIMIT 1")
        self.execute('UPDATE menu_items SET price_cents=? WHERE id=?', (MAX_MONEY_CENTS, item))
        self.client.post('/orders/add', data={'table_no': 1, 'item_id': item, 'quantity': 2})
        self.assertEqual(self.value('SELECT count(*) FROM orders'), 0)
        self.assertEqual(self.value('SELECT count(*) FROM customer_tabs'), 0)

    def test_backup_failure_preserves_data_and_releases_write_lock(self):
        self.reserve()
        with patch('billiard_app.blueprints.settings.backup_database', side_effect=OSError('test backup failure')):
            response = self.client.post('/settings/reset-test-data', data={'confirm_scope': 'yes', 'confirmation': '永久刪除測試營運資料', 'confirmation_date': date.today().isoformat()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.value('SELECT count(*) FROM reservations'), 1)
        self.client.post('/sessions/start', data={'table_no': 1, 'mode': 'timed'})
        self.assertEqual(self.value('SELECT count(*) FROM sessions'), 1)

    def test_reset_blocks_new_session_until_backup_and_delete_finish(self):
        from billiard_app.blueprints.settings import backup_database
        backed_up, release, started = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def paused_backup():
            result = backup_database()
            backed_up.set()
            if not release.wait(5):
                raise RuntimeError('test timeout')
            return result
        def reset():
            try:
                self.client.post('/settings/reset-test-data', data={'confirm_scope': 'yes', 'confirmation': '永久刪除測試營運資料', 'confirmation_date': date.today().isoformat()})
            except Exception as error:
                errors.append(error)
        def open_table():
            try:
                self.app.test_client().post('/sessions/start', data={'table_no': 10, 'mode': 'timed'})
            except Exception as error:
                errors.append(error)
            finally:
                started.set()
        with patch('billiard_app.blueprints.settings.backup_database', side_effect=paused_backup):
            reset_thread = threading.Thread(target=reset)
            reset_thread.start()
            self.assertTrue(backed_up.wait(5))
            open_thread = threading.Thread(target=open_table)
            open_thread.start()
            try:
                self.assertFalse(started.wait(0.2))
            finally:
                release.set()
                reset_thread.join(5)
                open_thread.join(5)
        self.assertFalse(errors)
        self.assertTrue(started.is_set())
        self.assertEqual(self.value("SELECT count(*) FROM sessions WHERE status='active' AND table_no=10"), 1)

    def test_shrink_and_start_do_not_hide_active_table(self):
        barrier = threading.Barrier(3)
        errors = []
        def shrink():
            try:
                barrier.wait()
                self.client.post('/settings/rates', data={'table_count': 9})
            except Exception as error:
                errors.append(error)
        def start():
            try:
                barrier.wait()
                self.app.test_client().post('/sessions/start', data={'table_no': 10, 'mode': 'timed'})
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=shrink), threading.Thread(target=start)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        count = int(self.value("SELECT value FROM settings WHERE key='table_count'"))
        self.assertEqual(self.value("SELECT count(*) FROM customer_tabs WHERE status!='closed' AND table_no>?", (count,)), 0)

    def test_timed_session_transfer_preserves_timer_rate_and_bill(self):
        self.execute('UPDATE table_rates SET timed_rate_per_min_cents=333 WHERE table_no=1')
        self.execute('UPDATE table_rates SET timed_rate_per_min_cents=999 WHERE table_no=2')
        self.client.post('/sessions/start', data={'table_no': 1, 'mode': 'timed'})
        session_id = self.value("SELECT id FROM sessions WHERE table_no=1 AND status='active'")
        before = self.value('SELECT start_time FROM sessions WHERE id=?', (session_id,))
        response = self.client.post(
            f'/sessions/{session_id}/transfer',
            data={'from_table': 1, 'to_table': 2},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/tables/2'))
        with self.app.app_context():
            session = get_db().execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            tab = get_db().execute('SELECT * FROM customer_tabs WHERE id=?', (session['customer_tab_id'],)).fetchone()
        self.assertEqual(session['table_no'], 2)
        self.assertEqual(tab['table_no'], 2)
        self.assertEqual(session['start_time'], before)
        self.assertEqual(session['rate_per_min_cents'], 333)
        self.assertEqual(self.value('SELECT count(*) FROM session_transfers WHERE session_id=?', (session_id,)), 1)
        self.client.post('/sessions/start', data={'table_no': 1, 'mode': 'timed'})
        self.assertEqual(self.value("SELECT count(*) FROM sessions WHERE status='active'"), 2)

    def test_package_transfer_keeps_orders_payments_discount_and_history(self):
        self.client.post(
            '/sessions/start',
            data={'table_no': 3, 'mode': 'package', 'package_hours': 2,
                  'discount_percent': '90', 'discount_scope': 'all'},
        )
        session_id = self.value("SELECT id FROM sessions WHERE table_no=3 AND status='active'")
        item_id = self.value("SELECT m.id FROM menu_items m JOIN categories c ON c.id=m.category_id WHERE c.name!='飲料' LIMIT 1")
        self.client.post('/orders/add', data={'table_no': 3, 'item_id': item_id, 'quantity': 2})
        payment_before = self.value('SELECT count(*) FROM payments WHERE session_id=?', (session_id,))
        discount_before = self.value('SELECT discount_percent FROM sessions WHERE id=?', (session_id,))
        self.client.post(f'/sessions/{session_id}/transfer', data={'from_table': 3, 'to_table': 4})
        self.assertEqual(self.value('SELECT table_no FROM sessions WHERE id=?', (session_id,)), 4)
        self.assertEqual(self.value('SELECT count(*) FROM orders WHERE session_id=?', (session_id,)), 1)
        self.assertEqual(self.value('SELECT count(*) FROM payments WHERE session_id=?', (session_id,)), payment_before)
        self.assertEqual(self.value('SELECT discount_percent FROM sessions WHERE id=?', (session_id,)), discount_before)
        detail = self.client.get('/tables/4').get_data(as_text=True)
        self.assertIn('3 號桌 → 4 號桌', detail)
        self.client.post(f'/sessions/end/{session_id}', data={'discount_percent': '90', 'discount_scope': 'all'})
        self.assertEqual(self.value('SELECT status FROM sessions WHERE id=?', (session_id,)), 'closed')
        self.assertEqual(self.value('SELECT table_no FROM sessions WHERE id=?', (session_id,)), 4)

    def test_transfer_rejects_occupied_or_stale_destination_without_partial_change(self):
        self.client.post('/sessions/start', data={'table_no': 1, 'mode': 'timed'})
        first_id = self.value("SELECT id FROM sessions WHERE table_no=1 AND status='active'")
        self.client.post('/sessions/start', data={'table_no': 2, 'mode': 'timed'})
        self.client.post(f'/sessions/{first_id}/transfer', data={'from_table': 1, 'to_table': 2})
        self.client.post(f'/sessions/{first_id}/transfer', data={'from_table': 9, 'to_table': 3})
        self.assertEqual(self.value('SELECT table_no FROM sessions WHERE id=?', (first_id,)), 1)
        self.assertEqual(self.value('SELECT count(*) FROM session_transfers WHERE session_id=?', (first_id,)), 0)

    def test_only_one_concurrent_transfer_can_claim_an_empty_table(self):
        for table_no in (1, 2):
            self.client.post('/sessions/start', data={'table_no': table_no, 'mode': 'timed'})
        ids = [self.value("SELECT id FROM sessions WHERE table_no=? AND status='active'", (table_no,)) for table_no in (1, 2)]
        barrier = threading.Barrier(3)
        errors = []
        def transfer(session_id, source):
            try:
                barrier.wait()
                self.app.test_client().post(
                    f'/sessions/{session_id}/transfer',
                    data={'from_table': source, 'to_table': 3},
                )
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=transfer, args=values) for values in zip(ids, (1, 2))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(self.value("SELECT count(*) FROM sessions WHERE table_no=3 AND status='active'"), 1)
        self.assertEqual(self.value("SELECT count(*) FROM sessions WHERE status='active'"), 2)
        self.assertEqual(self.value('SELECT count(*) FROM session_transfers'), 1)

    def test_shift_export_can_filter_each_employee_and_includes_inactive_history(self):
        alice = self.execute("INSERT INTO employees (name, is_active) VALUES ('Alice', 0)")
        shift_type = self.value('SELECT min(id) FROM shift_types')
        self.execute(
            """INSERT INTO shifts (employee_id, shift_type_id, staff_name, shift_type_name,
                                    shift_type, start_time, end_time)
               VALUES (?, ?, 'Alice', '晚班', '晚班', '2026-09-22T21:00', '2026-09-23T02:00')""",
            (alice, shift_type),
        )
        page = self.client.get('/shifts?month=2026-09&date=2026-09-22').get_data(as_text=True)
        self.assertIn('id="export-employee"', page)
        self.assertIn(f'<option value="{alice}">Alice</option>', page)
        self.assertIn("String(shift.employee_id) === employeeId", page)
        self.assertIn("（隔天）", page)
        self.assertIn("safeEmployeeName", page)

    def test_free_practice_marks_table_in_use_without_charge_or_payment(self):
        response = self.client.post(
            '/sessions/start', data={'table_no': 5, 'mode': 'practice'}
        )
        self.assertEqual(response.status_code, 302)
        session_id = self.value(
            "SELECT id FROM sessions WHERE table_no=5 AND status='active'"
        )
        self.assertEqual(
            self.value('SELECT is_free_practice FROM sessions WHERE id=?', (session_id,)),
            1,
        )
        self.assertEqual(
            self.value('SELECT rate_per_min_cents FROM sessions WHERE id=?', (session_id,)),
            0,
        )

        dashboard = self.client.get('/').get_data(as_text=True)
        detail = self.client.get('/tables/5').get_data(as_text=True)
        self.assertIn('免費練習', dashboard)
        self.assertIn('使用中', dashboard)
        self.assertIn('使用狀態', detail)
        self.assertIn('球檯費：0 元', detail)
        self.assertNotIn('計時結帳', detail)

        self.client.post(f'/sessions/end/{session_id}')
        self.assertEqual(
            self.value('SELECT status FROM sessions WHERE id=?', (session_id,)),
            'closed',
        )
        self.assertEqual(
            self.value('SELECT final_total_cents FROM sessions WHERE id=?', (session_id,)),
            0,
        )
        self.assertEqual(
            self.value('SELECT count(*) FROM payments WHERE session_id=?', (session_id,)),
            0,
        )

    def test_free_practice_close_charges_only_unpaid_food(self):
        self.client.post('/sessions/start', data={'table_no': 6, 'mode': 'practice'})
        session_id = self.value(
            "SELECT id FROM sessions WHERE table_no=6 AND status='active'"
        )
        item_id = self.value(
            """SELECT m.id FROM menu_items m
               JOIN categories c ON c.id = m.category_id
               WHERE c.name != '飲料' ORDER BY m.id LIMIT 1"""
        )
        unit_price = self.value(
            'SELECT price_cents FROM menu_items WHERE id=?', (item_id,)
        )
        self.client.post(
            '/orders/add',
            data={'table_no': 6, 'item_id': item_id, 'quantity': 2},
        )

        self.client.post(f'/sessions/end/{session_id}')
        expected = unit_price * 2
        self.assertEqual(
            self.value('SELECT table_fee_cents FROM sessions WHERE id=?', (session_id,)),
            0,
        )
        self.assertEqual(
            self.value('SELECT final_total_cents FROM sessions WHERE id=?', (session_id,)),
            expected,
        )
        self.assertEqual(
            self.value('SELECT table_fee_cents FROM payments WHERE session_id=?', (session_id,)),
            0,
        )
        self.assertEqual(
            self.value('SELECT food_fee_cents FROM payments WHERE session_id=?', (session_id,)),
            expected,
        )
        self.assertEqual(
            self.value('SELECT amount_cents FROM payments WHERE session_id=?', (session_id,)),
            expected,
        )

        stats = self.client.get(
            f'/stats?date={current_business_day().isoformat()}'
        ).get_data(as_text=True)
        self.assertIn('免費練習餐飲結帳', stats)

    def test_free_practice_transfer_preserves_free_state(self):
        self.client.post('/sessions/start', data={'table_no': 7, 'mode': 'practice'})
        session_id = self.value(
            "SELECT id FROM sessions WHERE table_no=7 AND status='active'"
        )
        self.client.post(
            f'/sessions/{session_id}/transfer',
            data={'from_table': 7, 'to_table': 8},
        )
        self.assertEqual(
            self.value('SELECT table_no FROM sessions WHERE id=?', (session_id,)), 8
        )
        self.assertEqual(
            self.value('SELECT is_free_practice FROM sessions WHERE id=?', (session_id,)),
            1,
        )
        detail = self.client.get('/tables/8').get_data(as_text=True)
        self.assertIn('免費練習', detail)
        self.assertIn('使用中', detail)
