#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json
import os
import traceback

from flask import Flask, jsonify, request

import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)


def postgres_dsn():
    """
    Returns the DSN string for connecting with postgres
    """
    return os.environ.get('POSTGRES_DSN')


class SimplePostgres:
    """
    Provides a cursor to a connection to a Postgres instance
    in a 'with' context.
    """

    def __enter__(self):
        """
        Starts a connection to a Postgres instance
        """
        self.conn = psycopg2.connect(
            postgres_dsn(),
            cursor_factory=RealDictCursor
        )
        self.cursor = self.conn.cursor()
        return self.cursor

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.conn.commit()
        # There's a global exception listener, so we want to re-raise
        # the exceptions.
        # However, we need to make sure that all connections and cursors
        # are properly closed
        except Exception as e:
            raise e
        finally:
            self.cursor.close()
            self.conn.close()


@app.route('/execute', methods=['post'])
def execute():
    req = request.json
    query = req['query']
    args = req.get('data', {})
    with SimplePostgres() as cur:
        cur.execute(query, args)
        return jsonify(cur.fetchall())


class InsertBuilder:
    """
    Builds an insert query string for one or more insert instructions
    """

    def __init__(self):
        self.params = []
        self.value_strs = []
        self.first_item = None

    def add(self, values):
        items = sorted(values.items())
        # save the first item for the column names
        if self.first_item is None:
            self.first_item = items

        self.params.extend(map(lambda x: x[1], items))
        self.value_strs.append(f"({','.join(map(lambda x: '%s', values))})")

    def names(self):
        return ','.join(map(lambda x: f'"{x[0]}"', self.first_item))

    def values(self):
        return ','.join(self.value_strs)


@app.route('/insert', methods=['post'])
def insert():
    builder = InsertBuilder()
    req = request.json
    table = req['table']
    values = req['values']
    # allow one or multiple insert entries
    if isinstance(values, list):
        for e in values:
            builder.add(e)
    else:
        builder.add(values)
    sql = (f"INSERT INTO {table} ({builder.names()}) "
           f"VALUES {builder.values()} RETURNING *")
    with SimplePostgres() as cur:
        cur.execute(sql, builder.params)
        if isinstance(values, list):
            return jsonify(cur.fetchall())
        else:
            return jsonify(cur.fetchone())


class QueryBuilder:
    """
    Builds a simple select query string
    """

    operators = {
        "$gt": ">",
        "$gte": ">=",
        "$lt": "<",
        "$lte": "<=",
        "$eq": "=",
    }

    def __init__(self):
        self.params = []
        self.current_column = None

    def group(self, value, action):
        query = self.build(value)
        return f" {action} ".join(query)

    def op(self, op, v):
        assert self.current_column is not None
        self.params.append(v)
        op = QueryBuilder.operators[op]
        return f'"{self.current_column}" {op} %s'

    def build(self, where):
        query = []
        for k, v in where.items():
            if k == '$and':
                query.append(self.group(v, 'AND'))
            elif k == '$or':
                query.append(self.group(v, 'OR'))
            else:
                if k in QueryBuilder.operators:
                    query.append(self.op(k, v))
                elif isinstance(v, dict):
                    assert self.current_column is None, \
                        f"Fields in '{self.current_column}' can't be nested."
                    old_current_column = self.current_column
                    self.current_column = k
                    query.append(self.group(v, 'AND'))
                    self.current_column = old_current_column
                else:
                    query.append(f'{k}=%s')
                    self.params.append(v)

        return query

    @staticmethod
    def build_query(where):
        if where is None or len(where) == 0:
            return {
                'query': '',
                'params': []
            }

        builder = QueryBuilder()
        query = builder.group(where, 'AND')
        return {
            'query': query,
            'params': builder.params
        }


@app.route('/delete', methods=['post'])
def delete():
    req = request.json
    table = req['table']
    where = req.get('where', None)

    where_str = ""
    query = QueryBuilder.build_query(where)
    if len(query['params']) > 0:
        where_str = f"WHERE ({query['query']}) "

    sql = f"DELETE FROM {table} {where_str} RETURNING *"
    with SimplePostgres() as cur:
        cur.execute(sql, query['params'])
        return jsonify(cur.fetchall())


def check_valid_sql_ident(ident):
    """
    https://www.postgresql.org/docs/9.6/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS
    """
    # For simplification diacritical marks and non-Latin-letters are rejected
    # for now.
    if not isinstance(ident, str) or len(ident) == 0:
        return False
    if not ident[0].isalnum():
        return False
    for c in ident:
        if not (c.isalnum() or c in "_$"):
            return False
    return True


@app.route('/select', methods=['post'])
def select():
    req = request.json
    table = req['table']
    where = req.get('where', None)
    columns = req.get('columns', None)

    if columns is None:
        columns = '*'
    else:
        assert isinstance(columns, list)
        # validate columns
        for column in columns:
            assert check_valid_sql_ident(column)
        columns = ','.join(f'"{col}"' for col in columns)

    sql = f"SELECT {columns} FROM {table}"
    query = QueryBuilder.build_query(where)
    if len(query['params']) > 0:
        sql += f" WHERE ({query['query']})"
    with SimplePostgres() as cur:
        cur.execute(sql, query['params'])
        return jsonify(cur.fetchall())


@app.route('/update', methods=['post'])
def update():
    req = request.json
    table = req['table']
    where = req.get('where', {})
    values = sorted(req['values'].items())
    update_params = list(map(lambda x: x[1], values))
    update_str = ','.join(map(lambda x: f'"{x[0]}" = %s', values))

    # use an optional select string
    query = QueryBuilder.build_query(where)
    update_params.extend(query['params'])
    where_str = ""
    if len(query['query']) > 0:
        where_str = f"WHERE ({query['query']}) "

    sql = (f"UPDATE {table} SET {update_str} {where_str}"
           "RETURNING *")
    with SimplePostgres() as cur:
        cur.execute(sql, update_params)
        return jsonify(cur.fetchall())


@app.route('/tables/drop', methods=['post'])
def tables_drop():
    req = request.json
    table = req['table']

    sql = f"DROP TABLE {table}"
    with SimplePostgres() as cur:
        cur.execute(sql)
        return jsonify([])


@app.route('/tables/create', methods=['post'])
def tables_create():
    req = request.json
    table = req['table']
    columns = req['columns']
    columns_params = []

    for k, v in sorted(columns.items()):
        columns_params.append(f"{k} {str(v)}")

    columns_str = ','.join(columns_params)
    sql = f"CREATE TABLE {table} ({columns_str})"
    with SimplePostgres() as cur:
        cur.execute(sql)
        return jsonify({})


@app.route('/health', methods=['get'])
def health():
    return 'OK'


def app_error(e):
    print(traceback.format_exc())
    return jsonify({'message': repr(e)}), 400


if __name__ == "__main__":
    assert 'POSTGRES_DSN' in os.environ, \
        "The environment variable 'POSTGRES_DSN' must be set."
    app.register_error_handler(Exception, app_error)
    app.run(host='0.0.0.0', port=8000)
