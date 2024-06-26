import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from airflow.hooks.base import BaseHook
from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator
import logging
from google.cloud import storage
import pandas as pd
import io
from sklearn.impute import KNNImputer
import numpy as np


def send_email_gmail(recipient, subject, body, **kwargs):
    # Fetch credentials from Airflow connection
    smtp_conn = BaseHook.get_connection('smtp_default')
    sender = smtp_conn.login
    password = smtp_conn.password

    # Set up the MIME
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    # Connect to Gmail's SMTP server
    server = smtplib.SMTP(host=smtp_conn.host, port=smtp_conn.port)
    server.starttls()
    server.login(sender, password)
    server.send_message(msg)
    server.quit()
    print(f"Email sent successfully to {recipient} with subject '{subject}'")


# Define your functions from clean_data.py
def download_from_gcs(bucket_name, source_blob_name, **kwargs):
    """Download file from GCS and return as a pandas DataFrame."""
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(source_blob_name)
        data = blob.download_as_string()
        df = pd.read_csv(io.BytesIO(data))
        logging.info(f"Successfully downloaded data from {bucket_name}/{source_blob_name}")
        return df
    except Exception as e:
        logging.error(f"Error downloading from GCS: {e}")
        raise

def upload_to_gcs(bucket_name, destination_blob_name, **kwargs):
    """Upload a pandas DataFrame to GCS."""
    try:
        ti = kwargs['ti']
        df = ti.xcom_pull(task_ids=kwargs['task_ids'][0])
        if df is not None:
            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(destination_blob_name)
            data_bytes = df.to_csv(index=False).encode()
            blob.upload_from_string(data_bytes, content_type='text/csv')
            logging.info(f"Uploaded data to {bucket_name}/{destination_blob_name}")
        else:
            raise ValueError("No data returned from XCom.")
    except Exception as e:
        logging.error(f"Error uploading to GCS: {e}")
        raise

def remove_missing_values_knn(**kwargs):
    """Replace missing values in the data using K-Nearest Neighbors."""
    ti = kwargs['ti']
    df = ti.xcom_pull(task_ids=kwargs['task_ids'][0])
    if df is not None:
        df.replace('?', np.nan, inplace=True)
        for col in df.columns:
            if col != 'Date':
                df[col] = pd.to_numeric(df[col], errors='coerce')
        df['Date'] = pd.to_datetime(df['Date'], format='%m/%d/%Y', errors='coerce')
        
        imputer = KNNImputer(n_neighbors=5)
        numerical_cols = df.select_dtypes(include=np.number).columns
        df[numerical_cols] = imputer.fit_transform(df[numerical_cols])
        
        logging.info(f"Missing values imputed using KNN for {kwargs['task_ids'][0]}")
        return df
    else:
        raise ValueError("No data returned from XCom.")

# Define the DAG
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2024, 6, 10),
    'email_on_failure': True,
    'email_on_retry': True,
    'retries': 1,
    'retry_delay': timedelta(minutes=2),
}

dag = DAG(
    'remove_missing_values_dag_KNN',
    default_args=default_args,
    description='DAG for removing missing values using KNN from ozone level data',
    schedule_interval='@daily',
    catchup=False,
)

# Define tasks to download data
download_eighthr_data = PythonOperator(
    task_id='download_eighthr_data',
    python_callable=lambda **kwargs: download_from_gcs('ozone_level_detection', 'data/raw/eighthr_data.csv', **kwargs),
    provide_context=True,
    dag=dag,
)

download_onehr_data = PythonOperator(
    task_id='download_onehr_data',
    python_callable=lambda **kwargs: download_from_gcs('ozone_level_detection', 'data/raw/onehr_data.csv', **kwargs),
    provide_context=True,
    dag=dag,
)

# Define tasks to remove missing values using KNN
remove_missing_values_eighthr = PythonOperator(
    task_id='remove_missing_values_eighthr',
    python_callable=remove_missing_values_knn,
    op_kwargs={'task_ids': ['download_eighthr_data']},
    provide_context=True,
    dag=dag,
)

remove_missing_values_onehr = PythonOperator(
    task_id='remove_missing_values_onehr',
    python_callable=remove_missing_values_knn,
    op_kwargs={'task_ids': ['download_onehr_data']},
    provide_context=True,
    dag=dag,
)

# Define tasks to upload cleaned data
upload_cleaned_eighthr = PythonOperator(
    task_id='upload_cleaned_eighthr',
    python_callable=lambda **kwargs: upload_to_gcs('ozone_level_detection', 'data/new_cleaned/eighthr_data_KNN.csv', **kwargs),
    op_kwargs={'task_ids': ['remove_missing_values_eighthr']},
    provide_context=True,
    dag=dag,
)

upload_cleaned_onehr = PythonOperator(
    task_id='upload_cleaned_onehr',
    python_callable=lambda **kwargs: upload_to_gcs('ozone_level_detection', 'data/new_cleaned/onehr_data_KNN.csv', **kwargs),
    op_kwargs={'task_ids': ['remove_missing_values_onehr']},
    provide_context=True,
    dag=dag,
)

email_task_1 = PythonOperator(
    task_id='send_email_1',
    python_callable=send_email_gmail,
    op_kwargs={
        'recipient': 'nikitamandal03@gmail.com',  
        'subject': 'Success',
        'body': 'Cleaned Eight hour data is uploaded to GCS Bucket.',
    },
    dag=dag,
)

email_task_2 = PythonOperator(
    task_id='send_email_2',
    python_callable=send_email_gmail,
    op_kwargs={
        'recipient': 'nikitamandal03@gmail.com',  
        'subject': 'Success',
        'body': 'Cleaned One hour data is uploaded to GCS Bucket.',
    },
    dag=dag,
)

# Set task dependencies
download_eighthr_data >> remove_missing_values_eighthr >> upload_cleaned_eighthr >> email_task_1
download_onehr_data >> remove_missing_values_onehr >> upload_cleaned_onehr >> email_task_2
