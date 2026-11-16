"""Shops are no longer classified by optional business ID or operator metadata."""

import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'releases/v22'))


class UnclassifiedShopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name);self.database=self.data/'shops.sqlite3'
        with closing(sqlite3.connect(self.database)) as db,db:
            db.executescript((ROOT/'sql/schema.sql').read_text(encoding='utf-8'))
            db.execute("INSERT INTO operators(id,name,name_key,created_at) VALUES(1,'OperatorOne','operatorone','2026-11-01')")
            for shop_id,name,business_id,operator_id in [(1,'ShopWithMetadata','BIZ-001',1),(2,'ShopWithoutMetadata',None,None)]:
                db.execute("INSERT INTO shops(id,name,slug_key,url,created_at,internal_shop_id,operator_id) VALUES(?,?,?,?,?,?,?)",(shop_id,name,name.lower(),f'https://www.etsy.com/shop/{name}','2026-11-01',business_id,operator_id))
        spec=importlib.util.spec_from_file_location('unclassified_shop_web',ROOT/'releases/v22/app.py')
        self.web=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.web
        self.addCleanup(lambda:sys.modules.pop(spec.name,None))
        with mock.patch.dict(os.environ,{'ETSY_TOOLS_DATA_DIR':str(self.data)}):spec.loader.exec_module(self.web)
        self.web.app.config.update(TESTING=True,SERVER_NAME='127.0.0.1:8765')
        self.client=self.web.app.test_client()

    def test_labels_and_scope_controls_are_absent_on_all_views(self):
        for route in ['/','/dashboard','/dashboard?metric=item_count','/dashboard?metric=daily_sales','/shops/1/data','/shops/2/data']:
            response=self.client.get(route);self.assertEqual(response.status_code,200)
            html=response.get_data(as_text=True)
            for retired in ['内部店铺','外部店铺','scope-tab','type-tag','name="scope"','scope=internal','scope=external']:
                self.assertNotIn(retired,html,(route,retired))
            if route == '/' or route.startswith('/shops/'):
                self.assertIn('data-delete-shop-form',html,route)

    def test_legacy_scope_parameters_cannot_exclude_any_shop(self):
        for metric in ['sales','item_count','daily_sales','bad_review_count']:
            for old_scope in ['all','internal','external','unknown']:
                with mock.patch.object(self.web,'render_template',return_value='ok') as render:
                    response=self.client.get(f'/dashboard?metric={metric}&scope={old_scope}')
                self.assertEqual(response.status_code,200)
                context=render.call_args.kwargs
                self.assertEqual([card['shop']['id'] for card in context['cards']],[1,2],(metric,old_scope))
                self.assertNotIn('scope',context)
                self.assertTrue(all('internal' not in card for card in context['cards']))

    def test_operator_filter_and_period_controls_still_work(self):
        with mock.patch.object(self.web,'render_template',return_value='ok') as render:
            self.client.get('/dashboard?operator=1&days=7&scope=external')
        context=render.call_args.kwargs
        self.assertEqual([card['shop']['id'] for card in context['cards']],[1])
        self.assertEqual(context['days'],7)
        self.assertEqual(context['operator_id'],'1')
        html=self.client.get('/dashboard?scope=internal').get_data(as_text=True)
        self.assertIn('id="operator-filter"',html)
        self.assertIn('最近 7 天',html)
        self.assertIn('最近 30 天',html)
        self.assertNotIn('scope=',html)

    def test_business_shop_id_and_operator_are_displayed_and_editable(self):
        html=self.client.get('/').get_data(as_text=True)
        self.assertIn('value="BIZ-001"',html)
        self.assertIn('value="OperatorOne"',html)
        html=self.client.get('/shops/1/data').get_data(as_text=True)
        self.assertIn('BIZ-001',html)
        self.assertIn('OperatorOne',html)
        with self.client.session_transaction() as session:session['csrf_token']='token'
        response=self.client.post('/shops/2/update',data={'csrf_token':'token','shop_id':'BIZ-002','operator':'OperatorTwo'})
        self.assertEqual(response.status_code,303)
        with self.web.connect_db() as db:
            row=db.execute('SELECT s.internal_shop_id,o.name FROM shops s LEFT JOIN operators o ON o.id=s.operator_id WHERE s.id=2').fetchone()
            self.assertEqual(tuple(row),('BIZ-002','OperatorTwo'))
        html=self.client.get('/shops/2/data').get_data(as_text=True)
        self.assertIn('BIZ-002',html)
        self.assertNotIn('内部店铺',html)
        self.assertNotIn('外部店铺',html)

    def test_visiting_pages_does_not_change_database_metadata_or_metrics(self):
        before=hashlib.sha256(self.database.read_bytes()).hexdigest()
        for route in ['/','/dashboard?scope=internal','/dashboard?scope=external','/shops/1/data','/shops/2/data']:
            self.assertEqual(self.client.get(route).status_code,200)
        self.assertEqual(before,hashlib.sha256(self.database.read_bytes()).hexdigest())


if __name__=='__main__':unittest.main()
