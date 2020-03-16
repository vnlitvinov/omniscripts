# coding: utf-8
import os
import sys
import argparse
import warnings
import time
import gzip
import mysql.connector
from timeit import default_timer as timer
import ibis

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from server import OmnisciServer
from server_worker import OmnisciServerWorker
from report import DbReport
from utils import import_pandas_into_module_namespace, execute_process, str_arg_to_bool

warnings.filterwarnings("ignore")

def compare_tables(table1, table2):
    
    if table1.equals(table2):
        return True
    else:
        print("\ntables are not equal, table1:")
        print(table1.info())
        print(table1)
        print("\ntable2:")
        print(table2.info())
        print(table2)
        return False

def compare_dataframes(ibis_df, pandas_df):
    comparison_result = True
    for i in range (len(ibis_df)):
        ibis_df[i].index = pandas_df[i].index
        comparison_result = comparison_result and ibis_df[i].equals(pandas_df[i])
        
    if comparison_result:
        print("Tables are equal")
        return True
    else:
        print("Tables are not equal:")
        diff = {}
        for i in range(len(ibis_df)):
            diff_df = ibis_df[i] - pandas_df[i]
            if (len(diff_df.shape) > 1):
                diff['DataFrame %s max deviation' % str(i + 1)] = diff_df.max().max()
                diff['DataFrame %s min deviation' % str(i + 1)] = diff_df.min().min()
            else:
                diff['DataFrame %s max deviation' % str(i + 1)] = diff_df.max()
                diff['DataFrame %s min deviation' % str(i + 1)] = diff_df.min()
            
        print("Values check summary:")
        for dev_type, value in diff.items():
            print(dev_type, ':', value)
        return False

# Dataset link
# https://www.kaggle.com/c/santander-customer-transaction-prediction/data

# Current script prerequisites:
# 1) Patched OmniSci version (https://github.com/intel-go/omniscidb/tree/ienkovich/santander)
# 2) Ibis version not older than e60d1af commit (otherwise apply ibis-santander.patch patch)

def load_data(
        filename, columns_names=None, columns_types=None, header=None, nrows=None, gzip=False
):
    types = None
    if columns_types:
        types = {columns_names[i]: columns_types[i] for i in range(len(columns_names))}
    open_method = gzip.open if gzip else open
    with open_method(filename) as f:
        return pd.read_csv(
            f, names=columns_names, nrows=nrows, header=header, dtype=types
        )


def etl_pandas(filename, columns_names, columns_types):
    etl_times = {
        "t_groupby_agg": 0.0,
        "t_drop": 0.0,
        "t_merge": 0.0,
        "t_readcsv": 0.0,
        "t_train_test_split": 0.0,
        "t_where": 0.0,
        "t_reset_index": 0.0,
        "t_assign_data": 0.0,
        "t_etl": 0.0,
    }
    
    t0 = timer()
    train_pd = load_data(filename=filename, columns_names=columns_names, columns_types=columns_types,
                   header=0, nrows=None, gzip=filename.endswith(".gz"))
    etl_times["t_readcsv"] = timer() - t0
    
    t_etl_begin = timer()

    for i in range(200):
        col = 'var_%d'%i
        t0 = timer()
        var_count = train_pd.groupby(col).agg({col:'count'})
        etl_times["t_groupby_agg"] += timer() - t0

        t0 = timer()
        var_count.columns = ['%s_count'%col]
        var_count = var_count.reset_index()
        etl_times["t_reset_index"] += timer() - t0

        t0 = timer()
        train_pd = train_pd.merge(var_count,on=col,how='left')
        etl_times["t_merge"] += timer() - t0

    for i in range(200):
        col = 'var_%d'%i

        t0 = timer()
        mask = train_pd['%s_count'%col]>1
        etl_times["t_where"] += timer() - t0

        t0 = timer()
        train_pd.loc[mask,'%s_gt1'%col] = train_pd.loc[mask,col]
        etl_times["t_assign_data"] += timer() - t0

    # train, test data split
    t0 = timer()
    train,valid = train_pd[:-10000],train_pd[-10000:]
    etl_times["t_train_test_split"] = timer() - t0

    t0 = timer()
    x_train = train.drop(['target','ID_code'],axis=1)
    etl_times["t_drop"] += timer() - t0

    y_train = train['target']

    t0 = timer()
    x_valid = valid.drop(['target','ID_code'],axis=1)
    etl_times["t_drop"] += timer() - t0

    y_valid = valid['target']

    etl_times["t_etl"] = timer() - t_etl_begin
    
    return x_train, y_train, x_valid, y_valid, etl_times

def etl_ibis(
    args,
    run_import_queries,
    columns_names,
    columns_types,
    validation=False
):
    
    filename=args.file
    database_name=args.name
    table_name=args.table
    delete_old_database=not args.dnd
    create_new_table=not args.dni
    
    tmp_table_name = 'tmp_table'

    etl_times = {
        "t_groupby_merge_where": 0.0,
        "t_train_test_split": 0.0,
        "t_etl": 0.0
    }
    
    if run_import_queries:
        etl_times_import = {
        "t_readcsv_by_ibis": 0.0,
        "t_readcsv_by_COPY": 0.0,
        "t_readcsv_by_FSI": 0.0
        }
        etl_times.update(etl_times_import)
    
    
    omnisci_server = OmnisciServer(
            omnisci_executable=args.omnisci_executable,
            omnisci_port=args.omnisci_port,
            database_name=args.name,
            user=args.user,
            password=args.password,
            debug_timer=True,
            columnar_output=args.server_columnar_output,
            lazy_fetch=args.server_lazy_fetch
    )

    omnisci_server.launch()

    omnisci_server_worker = OmnisciServerWorker(omnisci_server)
    
    time.sleep(2)
    conn_ipc = omnisci_server_worker.ipc_connect_to_server()
    conn = omnisci_server_worker.connect_to_server()

    omnisci_server_worker.create_database(
        database_name, delete_if_exists=delete_old_database
    )
    
    if run_import_queries:
         # SQL statemnts preparation for data file import queries
        connect_to_db_sql_template = "\c {0} admin HyperInteractive"
        create_table_sql_template = '''
        CREATE TABLE {0} ({1});
        '''
        import_by_COPY_sql_template = '''
        COPY {0} FROM '{1}' WITH (header='{2}');
        '''
        import_by_FSI_sql_template = '''
        CREATE TEMPORARY TABLE {0} ({1}) WITH (storage_type='CSV:{2}');
        '''
        drop_table_sql_template = '''
        DROP TABLE IF EXISTS {0};
        '''

        import_query_cols_list = ["ID_code TEXT ENCODING NONE, \n", "target SMALLINT, \n"] + [
            "var_%s DOUBLE, \n" % i for i in range(199)] + ["var_199 DOUBLE"]
        import_query_cols_str = "".join(import_query_cols_list)

        connect_to_db_sql = connect_to_db_sql_template.format(database_name)
        create_table_sql = create_table_sql_template.format(tmp_table_name, import_query_cols_str)
        import_by_COPY_sql = import_by_COPY_sql_template.format(tmp_table_name, filename, 'true')
        import_by_FSI_sql = import_by_FSI_sql_template.format(tmp_table_name, import_query_cols_str, filename)
        
        # data file import by ibis
        columns_types_import_query = ["string", "int64"] + ["float64" for _ in range(200)]
        t_import_pandas, t_import_ibis = omnisci_server_worker.import_data_by_ibis(
                table_name=tmp_table_name,
                data_files_names=filename,
                files_limit=1,
                columns_names=columns_names,
                columns_types=columns_types_import_query,
                header=0,
                nrows=None,
                compression_type=None
            )

        etl_times["t_readcsv_by_ibis"] = t_import_pandas + t_import_ibis

        # data file import by FSI
        omnisci_server_worker.drop_table(tmp_table_name)
        t0 = timer()
        omnisci_server_worker.execute_sql_query(import_by_FSI_sql)
        etl_times["t_readcsv_by_FSI"] = timer() - t0

        omnisci_server_worker.drop_table(tmp_table_name)

        # data file import by SQL COPY statement
        omnisci_server_worker.execute_sql_query(create_table_sql)

        t0 = timer()
        omnisci_server_worker.execute_sql_query(import_by_COPY_sql)
        etl_times["t_readcsv_by_COPY"] = timer() - t0

        omnisci_server_worker.drop_table(tmp_table_name)
    
    # Create table and import data for ETL queries
    if create_new_table:
        # Datafiles import
        omnisci_server_worker.import_data_by_ibis(
            table_name=table_name,
            data_files_names=filename,
            files_limit=1,
            columns_names=columns_names,
            columns_types=columns_types,
            header=0,
            nrows=None,
            compression_type=None
        )
    db = conn_ipc.database(database_name)
    #db = conn.database(database_name)
    table = db.table(table_name)

    # group_by/count, merge (join) and filtration queries
    # We are making 400 columns and then insert them into original table thus avoiding
    # nested sql requests
    
    if validation:
        t0 = timer()
        count_cols = []
        gt1_cols = []
        table_query = table['ID_code', 'target']
        for i in range(200):
            col = 'var_%d' % i
            col_count = 'var_%d_count' % i
            col_gt1 = 'var_%d_gt1' % i
            w = ibis.window(group_by=col)
            table_query = table_query.mutate(table[col].name(col))
            count_cols.append(table[col].count().over(w).name(col_count))
            gt1_cols.append(ibis.case().when(table[col].count().over(w).name(col_count) > 1,
                                             table[col]).else_(ibis.null()).end().name(col_gt1))

        table_query = table_query.mutate(count_cols)
        table_query = table_query.mutate(gt1_cols)

        table_df = table_query.execute()
        etl_times["t_groupby_merge_where"] = timer() - t0
        
    else:
        t0 = timer()
        count_cols = []
        gt1_cols = []
        table_query = table['ID_code', 'target']
        for i in range(200):
            col = 'var_%d' % i
            col_count = 'var_%d_count' % i
            col_gt1 = 'var_%d_gt1' % i
            w = ibis.window(group_by=col)
            table_query = table_query.mutate(table[col].cast("float32").name(col))
            count_cols.append(table[col].count().over(w).name(col_count))
            gt1_cols.append(ibis.case().when(table[col].count().over(w).name(col_count) > 1,
                                             table[col].cast("float32")).else_(ibis.null()).end().name(col_gt1))

        table_query = table_query.mutate(count_cols)
        table_query = table_query.mutate(gt1_cols)

        table_df = table_query.execute()
        etl_times["t_groupby_merge_where"] = timer() - t0
    
    # rows split query
    t0 = timer()
    training_part, validation_part = table_df[:-10000], table_df[-10000:]
    etl_times["t_train_test_split"] = timer() - t0
    
    etl_times["t_etl"] = etl_times["t_groupby_merge_where"] + etl_times["t_train_test_split"]
    
    x_train = training_part.drop(['target','ID_code'],axis=1)
    y_train = training_part['target']
    x_valid = validation_part.drop(['target','ID_code'],axis=1)
    y_valid = validation_part['target']
    
    omnisci_server.terminate()
    omnisci_server = None
    
    return x_train, y_train, x_valid, y_valid, etl_times

def print_times(etl_times, name=None):
    if name:
        print(f"{name} times:")
    for time_name, time in etl_times.items():
        print("{} = {:.5f} s".format(time_name, time))

def print_times_nested(etl_times, name=None):
    if name:
        print(f"{name} times:")
    for meas_name, metrics in etl_times.items():
        print(meas_name + ':')
        for metric_name, time_value in metrics.items():
            print("    {} = {:.5f} s".format(metric_name, time_value))

def mse(y_test, y_pred):
    return ((y_test - y_pred) ** 2).mean()

def cod(y_test, y_pred):
    y_bar = y_test.mean()
    total = ((y_test - y_bar) ** 2).sum()
    residuals = ((y_test - y_pred) ** 2).sum()
    return 1 - (residuals / total)

def ml(x_train, y_train, x_valid, y_valid):
    
    import xgboost
    ml_times = {"t_ML": 0.0,
                "t_train": 0.0,
                "t_inference": 0.0,
                "t_dmatrix": 0.0
               }
    
    t0 = timer()
    training_dmat_part = xgboost.DMatrix(data=x_train, label=y_train)
    testing_dmat_part = xgboost.DMatrix(data=x_valid, label=y_valid)
    ml_times["t_dmatrix"] = timer() - t0
    
    watchlist = [(training_dmat_part, 'eval'), (testing_dmat_part, 'train')]
    xgb_params = {
            'objective': 'binary:logistic',
            'tree_method': 'hist',
            'max_depth': 1,
            'nthread':56,
            'eta':0.1,
            'silent':1,
            'subsample':0.5,
            'colsample_bytree': 0.05,
            'eval_metric':'auc',
    }
    
    t0 = timer()
    model = xgboost.train(xgb_params, dtrain=training_dmat_part,
                num_boost_round=10000, evals=watchlist,
                early_stopping_rounds=30, maximize=True,
                verbose_eval=1000)
    ml_times["t_train"] = timer() - t0

    t0 = timer()
    yp = model.predict(testing_dmat_part)
    ml_times["t_inference"] = timer() - t0

    score_mse = mse(y_valid, yp)
    score_cod = cod(y_valid, yp)
    
    ml_times["t_ML"] += ml_times["t_train"] + ml_times["t_inference"]
    
    return score_mse, score_cod, ml_times

def query_measurement_etl(query_function, query_func_args, iterations_number, query_name):
    meas_results = {}
    times_sum = {}

    for iteration in range(1, iterations_number + 1):
        print("Running", query_name, ", iteration", iteration)
        
        if iteration == iterations_number:
            x_train, y_train, x_valid, y_valid, cur_results = query_function(**query_func_args)
        else:
            _, _, _, _, cur_results = query_function(**query_func_args)
            
        for key, value in cur_results.items():
            if iteration == 1:
                meas_results[key] = {'first_exec_time': value}
                meas_results[key].update({'best_exec_time': float("inf")})
                meas_results[key].update({'worst_exec_time': 0.0})
                times_sum[key] = value
            if meas_results[key]['best_exec_time'] > value:
                meas_results[key]['best_exec_time'] = value
            if meas_results[key]['worst_exec_time'] < value:
                meas_results[key]['worst_exec_time'] = value
            
            if iteration != 1:
                times_sum[key] += value
                
            if iteration == iterations_number:
                meas_results[key].update({'average_exec_time': times_sum[key] / iterations_number})

    return x_train, y_train, x_valid, y_valid, meas_results

def query_measurement_ml(query_function, query_func_args, iterations_number, query_name):
    meas_results = {}
    times_sum = {}

    for iteration in range(1, iterations_number + 1):
        print("Running", query_name, ", iteration", iteration)
        
        if iteration == iterations_number:
            score_mse, score_cod, cur_results = query_function(**query_func_args)
        else:
            _, _, cur_results = query_function(**query_func_args)
            
        for key, value in cur_results.items():
            if iteration == 1:
                meas_results[key] = {'first_exec_time': value}
                meas_results[key].update({'best_exec_time': float("inf")})
                meas_results[key].update({'worst_exec_time': 0.0})
                times_sum[key] = value
            if meas_results[key]['best_exec_time'] > value:
                meas_results[key]['best_exec_time'] = value
            if meas_results[key]['worst_exec_time'] < value:
                meas_results[key]['worst_exec_time'] = value
            
            if iteration != 1:
                times_sum[key] += value
                
            if iteration == iterations_number:
                meas_results[key].update({'average_exec_time': times_sum[key] / iterations_number})

    return score_mse, score_cod, meas_results
        
def submit_results_to_db(db_reporter, args, backend, results):
    for meas_name, metrics in results.items():
        db_reporter.submit({
            'QueryName': str(meas_name),
            'FirstExecTimeMS': int(round(metrics['first_exec_time'] * 1000)),
            'WorstExecTimeMS': int(round(metrics['worst_exec_time'] * 1000)),
            'BestExecTimeMS': int(round(metrics['best_exec_time'] * 1000)),
            'AverageExecTimeMS': int(round(metrics['average_exec_time'] * 1000)),
            'TotalTimeMS': 0,
            'IbisCommitHash': args.commit_ibis,
            'BackEnd': str(backend)
        })

def main():
    omniscript_path = os.path.dirname(__file__)
    args = None
    omnisci_server = None

    parser = argparse.ArgumentParser(description="Run internal tests from ibis project")
    optional = parser._action_groups.pop()
    required = parser.add_argument_group("required arguments")
    parser._action_groups.append(optional)

    required.add_argument(
        "-f",
        "--file",
        dest="file",
        required=True,
        help="A datafile that should be loaded",
    )
    optional.add_argument("-dnd", action="store_true", help="Do not delete old table.")
    optional.add_argument(
        "-dni",
        action="store_true",
        help="Do not create new table and import any data from CSV files.",
    )
    optional.add_argument(
        "-val",
        action="store_true",
        help="validate queries results (by comparison with Pandas queries results).",
    )
    optional.add_argument(
        "-o",
        "--optimizer",
        choices=["intel", "stock"],
        dest="optimizer",
        default="intel",
        help="Which optimizer is used",
    )
    # MySQL database parameters
    optional.add_argument(
        "-db-server",
        dest="db_server",
        default="localhost",
        help="Host name of MySQL server.",
    )
    optional.add_argument(
        "-db-port",
        dest="db_port",
        default=3306,
        type=int,
        help="Port number of MySQL server.",
    )
    optional.add_argument(
        "-db-user",
        dest="db_user",
        default="",
        help="Username to use to connect to MySQL database. "
        "If user name is specified, script attempts to store results in MySQL "
        "database using other -db-* parameters.",
    )
    optional.add_argument(
        "-db-pass",
        dest="db_password",
        default="omniscidb",
        help="Password to use to connect to MySQL database.",
    )
    optional.add_argument(
        "-db-name",
        dest="db_name",
        default="omniscidb",
        help="MySQL database to use to store benchmark results.",
    )
    optional.add_argument(
        "-db-table",
        dest="db_table",
        help="Table to use to store results for this benchmark.",
    )
    # Omnisci server parameters
    optional.add_argument(
        "-e",
        "--executable",
        dest="omnisci_executable",
        required=False,
        help="Path to omnisci_server executable.",
    )
    optional.add_argument(
        "-w",
        "--workdir",
        dest="omnisci_cwd",
        help="Path to omnisci working directory. "
        "By default parent directory of executable location is used. "
        "Data directory is used in this location.",
    )
    optional.add_argument(
        "-port",
        "--omnisci_port",
        dest="omnisci_port",
        default=6274,
        type=int,
        help="TCP port number to run omnisci_server on.",
    )
    optional.add_argument(
        "-u",
        "--user",
        dest="user",
        default="admin",
        help="User name to use on omniscidb server.",
    )
    optional.add_argument(
        "-p",
        "--password",
        dest="password",
        default="HyperInteractive",
        help="User password to use on omniscidb server.",
    )
    optional.add_argument(
        "-n",
        "--name",
        dest="name",
        default="santander_database",
        help="Database name to use in omniscidb server.",
    )
    optional.add_argument(
        "-t",
        "--table",
        dest="table",
        default="santander_table",
        help="Table name name to use in omniscidb server.",
    )
    optional.add_argument(
        "--enable-columnar-output",
        dest="server_columnar_output",
        type=str_arg_to_bool,
        default=None,
        help="Launch OmniSci server with --enable-columnar-output option.",
    )
    optional.add_argument(
        "--enable-lazy-fetch",
        dest="server_lazy_fetch",
        type=str_arg_to_bool,
        default=None,
        help="Launch OmniSci server with --enable-lazy-fetch option.",
    )

    optional.add_argument(
        "-commit_omnisci",
        dest="commit_omnisci",
        default="1234567890123456789012345678901234567890",
        help="Omnisci commit hash to use for benchmark.",
    )
    optional.add_argument(
        "-commit_ibis",
        dest="commit_ibis",
        default="1234567890123456789012345678901234567890",
        help="Ibis commit hash to use for benchmark.",
    )
    optional.add_argument(
        "-no_ibis",
        action="store_true",
        help="Do not run Ibis benchmark, run only Pandas (or Modin) version"
    )
    optional.add_argument(
        "-pandas_mode",
        choices=["pandas", "modin_on_ray", "modin_on_dask"],
        default="pandas",
        help="Specifies which version of Pandas to use: plain Pandas, Modin runing on Ray or on Dask"
    )
    optional.add_argument(
        "-ray_tmpdir",
        default="/tmp",
        help="Location where to keep Ray plasma store. It should have enough space to keep -ray_memory"
    )
    optional.add_argument(
        "-ray_memory",
        default=200*1024*1024*1024,
        help="Size of memory to allocate for Ray plasma store"
    )
    optional.add_argument(
        "-no_ml",
        action="store_true",
        help="Do not run machine learning benchmark, only ETL part"
    )
    optional.add_argument(
        "-i",
        "--iterations",
        dest="iterations",
        default=1,
        type=int,
        help="Number of iterations to run every query. Best result is selected.",
    )
    

    args = parser.parse_args()
    args.file = args.file.replace("'", "")
    
    data_file_name, data_file_ext = os.path.splitext(args.file)
    if data_file_ext != '.csv':
        csv_data_file = data_file_name
        if not os.path.exists(data_file_name):
            execute_process(cmdline=['tar', '-xvf', args.dp, '--strip', '1'],
                            cwd=pathlib.Path(args.file).parent)

    var_cols = ["var_%s"%i for i in range(200)]
    count_cols = ["var_%s_count"%i for i in range(200)]
    gt1_cols = ["var_%s_gt1"%i for i in range(200)]
    columns_names = ["ID_code", "target"] + var_cols
    columns_types_pd = ["object", "int64"] + ["float64" for _ in range(200)]
    columns_types_ibis = ["string", "int64"] + ["decimal(8, 4)" for _ in range(200)]
    columns_types_ibis_val = ["string", "string"] + ["string" for _ in range(200)]
    columns_types_pd_val = ["object", "object"] + ["object" for _ in range(200)]

    try:
        db_reporter = None
        if args.db_user is not "":
            print("Connecting to database")
            db = mysql.connector.connect(host=args.db_server, port=args.db_port, user=args.db_user,
                                         passwd=args.db_pass, db=args.db_name)
            db_reporter = DbReport(db, args.db_table, {
                'QueryName': 'VARCHAR(500) NOT NULL',
                'FirstExecTimeMS': 'BIGINT UNSIGNED',
                'WorstExecTimeMS': 'BIGINT UNSIGNED',
                'BestExecTimeMS': 'BIGINT UNSIGNED',
                'AverageExecTimeMS': 'BIGINT UNSIGNED',
                'TotalTimeMS': 'BIGINT UNSIGNED',
                'IbisCommitHash': 'VARCHAR(500) NOT NULL',
                'BackEnd': 'VARCHAR(100) NOT NULL'
            }, {
                'ScriptName': 'santander_pandas_ibis.py',
                'CommitHash': args.commit_omnisci,
                'IbisCommitHash': args.commit_ibis
            })
        if not args.no_ibis:
            if args.omnisci_executable is None:
                parser.error("Omnisci executable should be specified with -e/--executable")


            etl_ibis_args = {'args': args, 'run_import_queries': "True",
                             'columns_names': columns_names, 'columns_types': columns_types_ibis}
            x_train_ibis, y_train_ibis, x_valid_ibis, y_valid_ibis, etl_times_ibis = query_measurement_etl(etl_ibis,
                                                                                                           etl_ibis_args,
                                                                                                           args.iterations,
                                                                                                           "etl_ibis")

            print_times_nested(etl_times_ibis, name='Ibis')
            if db_reporter is not None:
                submit_results_to_db(db_reporter=db_reporter, args=args, backend='etl_ibis', results=etl_times_ibis)

        import_pandas_into_module_namespace(main.__globals__,
                                            args.pandas_mode, args.ray_tmpdir, args.ray_memory)

        etl_pandas_args = {'filename': args.file, 'columns_names': columns_names, 'columns_types': columns_types_pd}
        x_train_pandas, y_train_pandas, x_valid_pandas, y_valid_pandas, etl_times_pandas = query_measurement_etl(etl_pandas,
                                                                                                                 etl_pandas_args,
                                                                                                                 args.iterations,
                                                                                                                 "etl_pandas")

        print_times_nested(etl_times_pandas, name=args.pandas_mode)

        if db_reporter is not None:
                submit_results_to_db(db_reporter=db_reporter, args=args, backend='etl_pandas', results=etl_times_pandas)

        if not args.no_ml:
            ml_args = {'x_train': x_train_pandas, 'y_train': y_train_pandas,
                       'x_valid': x_valid_pandas, 'y_valid': y_valid_pandas}
            score_mse_pandas, score_cod_pandas, ml_times_pandas = query_measurement_ml(ml,
                                                                                       ml_args,
                                                                                       args.iterations,
                                                                                       "ml")
            print('Scores with etl_pandas ML inputs: ')
            print('  mse = ', score_mse_pandas)
            print('  cod = ', score_cod_pandas)
            print_times_nested(ml_times_pandas)
            if db_reporter is not None:
                submit_results_to_db(db_reporter=db_reporter, args=args, backend='ml_pandas', results=ml_times_pandas)

        # Results validation block (comparison of etl_ibis and etl_pandas outputs)
        if args.val:
            print("Validation of ETL query results with input tables with original datatypes values ...")

            print("Validating queries results (var_xx columns) ...")
            compare_result1 = compare_dataframes(ibis_df=(x_train_ibis[var_cols], x_valid_ibis[var_cols]),
                                                 pandas_df=(x_train_pandas[var_cols], x_valid_pandas[var_cols]))
            print("Validating queries results (var_xx_count columns) ...")
            compare_result2 = compare_dataframes(ibis_df=(x_train_ibis[count_cols], x_valid_ibis[count_cols]),
                                                 pandas_df=(x_train_pandas[count_cols], x_valid_pandas[count_cols]))
            print("Validating queries results (var_xx_gt1 columns) ...")
            compare_result3 = compare_dataframes(ibis_df=(x_train_ibis[gt1_cols], x_valid_ibis[gt1_cols]),
                                                 pandas_df=(x_train_pandas[gt1_cols], x_valid_pandas[gt1_cols]))

            if not (compare_result1 and compare_result2 and compare_result3):
                print("ETL query results with input tables with original datatypes values are not equal, \
    checking query results with input tables with string datatypes values ...")

                x_train_ibis_val, _, x_valid_ibis_val, _, _ = etl_ibis(
                        args=args,
                        run_import_queries=False,
                        columns_names=columns_names,
                        columns_types=columns_types_ibis_val,
                        validation=True
                )

                x_train_pandas_val, _, x_valid_pandas_val, _, _ = etl_pandas(
                    args.file,
                    columns_names=columns_names,
                    columns_types=columns_types_pd_val
                )

                print("Validating queries results (var_xx columns) ...")
                compare_result1 = compare_dataframes(ibis_df=(x_train_ibis_val[var_cols], x_valid_ibis_val[var_cols]),
                                                     pandas_df=(x_train_pandas_val[var_cols], x_valid_pandas_val[var_cols]))
                print("Validating queries results (var_xx_count columns) ...")
                compare_result2 = compare_dataframes(ibis_df=(x_train_ibis_val[count_cols], x_valid_ibis_val[count_cols]),
                                                     pandas_df=(x_train_pandas_val[count_cols], x_valid_pandas_val[count_cols]))
                print("Validating queries results (var_xx_gt1 columns) ...")
                compare_result3 = compare_dataframes(ibis_df=(x_train_ibis_val[gt1_cols], x_valid_ibis_val[gt1_cols]),
                                                     pandas_df=(x_train_pandas_val[gt1_cols], x_valid_pandas_val[gt1_cols]))

    except Exception as err:
        print("Failed: ", err)
        sys.exit(1)
    finally:
        if omnisci_server:
            omnisci_server.terminate()

if __name__ == "__main__":
    main()
