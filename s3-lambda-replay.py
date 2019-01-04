"""s3-lambda-replay
A simple tool for replaying S3 file creation Lambda invocations. This is useful
for backfill or replay on real-time ETL pipelines that run transformations in
Lambdas triggered by S3 file creation events.

Steps:
    1. Collect inputs from user
    2. Scan S3 for filenames that need to be replayed
    3. Batch S3 files into payloads for Lambda invocations
    4. Spawn workers to handle individual Lambda invocations/retries
    5. Process the work queue, keeping track of progesss in a file in case of interrupts
"""
from multiprocessing import Queue, Process

import boto3
import json
import questionary
import time

from util.config import ReplayConfig

s3 = boto3.client('s3')
lambda_client = boto3.client('lambda')

class LambdaWorker(Process):
    def __init__(self, job_queue, result_queue, id, total_jobs):
        super(LambdaWorker, self).__init__()
        self.job_queue = job_queue
        self.result_queue = result_queue
        self.id = id
        self.total_jobs = total_jobs

    def run(self):
        for job in iter(self.job_queue.get, None):
            print(f"Worker {self.id} - Job {job['id']}/{self.total_jobs}")
            results = {
                'id': job['id'],
                'body': '',
                'status': 500,
                'retries': 0,
                'error': '',
            }

            while True:
                try:
                    response = lambda_client.invoke(
                        FunctionName=job['lambda'],
                        Payload=json.dumps(job['data']).encode('utf-8')
                    )

                    results['body'] = response['Payload'].read().decode('utf-8')
                    results['status'] = response['StatusCode']
                    results['error'] = response['FunctionError']
                    print(f"Worker {self.id} - Response {results['status']} {results['body']}")

                except Exception as e:
                    print(f"Worker {self.id} - Exception caught {e}")
                    results['body'] = str(e)
                    if results['retries'] >= 5:
                        results['error'] = 'TooManyRetries'
                        break

                    results['retries'] += 1

                    # Exp Backoff, 200ms, 400ms, 800ms, 1600ms
                    time.sleep((2**results['retries']) * 0.1)
                    print(f"Worker {self.id} - Attempt {results['retries']}/5")

            # Report the results back to the master process
            result_queue.put(results)

        # Sentinel to let the master know the worker is done
        result_queue.put(None)

def s3_object_generator(bucket, prefix=''):
    """ Generate objects in an S3 bucket."""
    opts = {
        'Bucket': bucket,
        'Prefix': prefix,
    }
    while True:
        resp = s3.list_objects_v2(**opts)
        contents = resp['Contents']

        for obj in contents:
            yield obj

        try:
            opts['ContinuationToken'] = resp['NextContinuationToken']
        except KeyError:
            break

def generate_sns_lambda_payload(files):
    return {'Records': [
        {
            'EventSource': 'aws:sns',
            'EventVersion': '1.0',
            'EventSubscriptionArn': 'arn:aws:sns:us-west-2:0000:s3-sns-lambda-replay-XXXX:1234-123-12-12',
            'Sns': {
                'SignatureVersion': "1",
                'Timestamp': "1970-01-01T00:00:00.000Z",
                'Signature': "replay",
                'SigningCertUrl': "replay",
                'MessageId': "95df01b4-ee98-5cb9-9903-4c221d41eb5e",
                'Message': json.dumps({"Records": files}),
                'MessageAttributes': {},
                'Type': "Notification",
                'UnsubscribeUrl': "replay",
                'TopicArn': "",
                'Subject': "ReplayInvoke",
            }
        }
    ]}

def pull_jobs(config):
    files = []
    for path in config.s3_paths:
        for obj in s3_object_generator(config.s3_bucket, path):
            files.append({'s3': {
                'bucket': {'name': config.s3_bucket},
                'object': {'key': obj['Key']},
            }})

    jobs = []
    while len(files) > 0:
        batch = files[0:config.batch_size]
        data = generate_sns_lambda_payload(batch)

        for func in config.lambda_functions:
            jobs.append({
                'lambda': func,
                'data': data,
                'id': len(jobs),
                'result': None,
            })

        # Move on to the next batch
        files = files[config.batch_size:]

    return jobs

def log_state(jobs, failed_jobs):
    with open('jobs.json', 'w+') as fh:
        fh.write(json.dumps(jobs))
    with open('jobs-failed.json', 'w+') as fh:
        fh.write(json.dumps(failed_jobs))

if __name__ == "__main__":
    config = ReplayConfig()
    print(config)
    if not questionary.confirm("Is this configuration correct?", default=False).ask():
        exit()

    jobs = pull_jobs(config)
    failed_jobs = []
    log_state(jobs, failed_jobs)

    workers = []
    job_queue = Queue()
    result_queue = Queue()

    for i in range(config.concurrency):
        worker = LambdaWorker(job_queue, result_queue, i, len(jobs))
        workers.append(worker)
        worker.start()

    for job in jobs:
        job_queue.put(job)

    # Add sentinels to the queue for each of our workers
    for i in range(config.concurrency):
        job_queue.put(None)

    # Collect worker results
    completed_workers = 0
    while completed_workers < config.concurrency:
        result = result_queue.get()
        if result is None:
            completed_workers += 1
            continue

        jobs[result['id']]['result'] = result
        if result['error'] != '':
            failed_jobs.append(jobs[result['id']])

        log_state(jobs, failed_jobs)

    # Wait for processes to finish
    for worker in workers:
        worker.join()

    print("Replay Complete!")
