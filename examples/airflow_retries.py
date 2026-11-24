from datetime import timedelta
import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator


def fail_once(ti):
    if ti.try_number == 1:
        raise RuntimeError("First attempt fails intentionally")
    return "recovered"


with DAG("datalog_retries", start_date=pendulum.datetime(2026, 11, 1, tz="UTC"), schedule=None, catchup=False) as dag:
    PythonOperator(task_id="retry_once", python_callable=fail_once, retries=1, retry_delay=timedelta(0))
if __name__ == "__main__":
    dag.test()
