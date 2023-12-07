import os
import sys
import time
from dotenv import load_dotenv
from celery import Celery
from celery.result import AsyncResult
from sqlalchemy import insert, select
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import sessionmaker
from src.database import user_credit

# OpenTelemetry imports for tracing and metrics
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor

# Logging imports
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
import logging

OTEL_ENDPOINT = os.getenv('OTEL_ENDPOINT', "otel-collector:4317")
service_name = "payment_worker"

# Initialize TracerProvider for OTLP
resource = Resource(attributes={SERVICE_NAME: service_name})
trace_provider = TracerProvider(resource=resource)
otlp_trace_exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
trace_provider.add_span_processor(BatchSpanProcessor(otlp_trace_exporter))
trace.set_tracer_provider(trace_provider)

# Initialize MeterProvider for OTLP
metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True))
metric_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(metric_provider)

# Initialize LoggerProvider for OTLP
logger_provider = LoggerProvider(resource=resource)
set_logger_provider(logger_provider)
otlp_log_exporter = OTLPLogExporter(endpoint=OTEL_ENDPOINT, insecure=True)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(otlp_log_exporter))
handler = LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)

# Attach OTLP handler to root logger
logging.getLogger().addHandler(handler)

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

BROKER_URL = os.getenv("CELERY_BROKER_URL")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND")
celery_app = Celery('create_payment', broker=BROKER_URL,
                    backend=RESULT_BACKEND)
DATABASE_URL_PAYMENT = os.getenv("DATABASE_URL_PAYMENT")
engine = create_engine(DATABASE_URL_PAYMENT)
Session = sessionmaker(bind=engine)

CeleryInstrumentor().instrument()

payment_counter = meter.create_counter(
    "payment_created",
    description="Total number of payment created",
    unit="1",
)
payment_commit_counter = meter.create_counter(
    "payment_committed",
    description="Total number of committed payments",
    unit="1",
)
payment_rollback_counter = meter.create_counter(
    "payment_rollback",
    description="Total number of rollback payments",
    unit="1",
)
credit_check_counter = meter.create_counter(
    "credit_checks",
    description="Total number of credit checks",
    unit="1",
)
inventory_results_counter = meter.create_counter(
    "inventory_results_waited",
    description="Counts how many times inventory results were waited for",
    unit="1",
)
# Let assume new user has 100 credit
# 1 late day cost 10 credit
@celery_app.task(name="create_payment")
def create_payment(payload: dict, fn: str):
    with tracer.start_as_current_span("create_payment_task"):
        logger.info("Creating payment", extra={"payload": payload, "function": fn})
        payment_counter.add(1)

        print("fn="+str(fn))
        print("payload="+str(payload))
        username: str = payload.get("username")
        quantity: int = payload.get("quantity")
        delivery: bool = payload.get("delivery")
        print("username="+str(username))
        print("quantity="+str(quantity))
        print("delivery="+str(delivery))
        print("db_url="+str(DATABASE_URL_PAYMENT))
        logger.info(f"Payment details: username={username}, quantity={quantity}, delivery={delivery}")
        if fn == "pay":
            print("checking payment credit")
            credit_check_counter.add(1)
            session = Session()
            try:
                # query to check if username exist in db if not create new user with 100 credit
                logger.info("Checking if user exists in the database")
                query = select([user_credit]).where(user_credit.c.username == username)
                result = session.execute(query)
                print("result="+str(result))
                print("result.rowcount = "+str(result.rowcount))
                if result.rowcount == 0:
                    logger.info("Creating new user")
                    query = insert(user_credit).values(username=username, credit=100)
                    session.execute(query)
                    session.commit()
                else:
                    logger.info("User is alreay exist")

                # Query to check if user credit is enough to pay
                query = select([user_credit]).where(user_credit.c.username == username)
                credit_amt = session.execute(query).fetchone()  # Fetch the result row
                print("result_row=" + str(credit_amt))
                if credit_amt.credit < quantity*10:
                    print("not enough credit")
                    logger.info("Not enough credit")
                    celery_app.send_task("create_order", queue='q01', args=[payload, "rollback_order"])
                    return "INSUFFICIENT_FUND"                
                else:
                    print("enough credit")
                    logger.info("Enough credit")
                    commit_payment(username, quantity, delivery)
                    print("payment committed successfully")
                    inventory_task = celery_app.send_task("update_inventory", queue='q03', args=[payload, "update_inventory"])
                    #returning result to the order service
                    print("inventory_task_id="+str(inventory_task.id))
                    return waiting_inventory_result(inventory_task.id)

            except Exception as e:
                logger.error(f"Error during database operation: {e}")
            finally:
                session.close()
        elif fn == "rollback_payment":
            rollback_payment(username, quantity, delivery)
            celery_app.send_task("create_order", queue='q01', args=[payload, "rollback_order"])
            print("payment service send task to rollback order")
            logger.info("Payment service send task to rollback order")
        else:
            print("invalid function name in payment service kub")
            logger.error("Invalid function name in payment service")

@celery_app.task
def waiting_inventory_result(inventory_task_id):
    with tracer.start_as_current_span("waiting_inventory_result"):
        time.sleep(1.2)
        inventory_task_result = AsyncResult(inventory_task_id)
        inventory_results_counter.add(1)
        if inventory_task_result.ready():
            result_value = inventory_task_result.result
            logger.info(f"Task result: {result_value}")
            return result_value
        else:
            logger.info("Inventory task is still running...")
            return "inventory task is still running..."
    
@celery_app.task
def commit_payment(username: str, quantity: int, delivery: bool):
    with tracer.start_as_current_span("commit_create_payment"):
        print("commiting payment")
        session = Session()
        try:
            deduct_amt = quantity*10
            query = user_credit.update().where(user_credit.c.username == username).values(credit=user_credit.c.credit - deduct_amt)
            session.execute(query)
            session.commit()
            logger.info("Payment committed", extra={"username": username, "quantity": quantity, "delivery": delivery})
            payment_commit_counter.add(1)
        except Exception as e:
            logger.error("Error during database operation", exc_info=True)
        finally:
            session.close()

@celery_app.task
def rollback_payment(username: str, quantity: int, delivery: bool):
    with tracer.start_as_current_span("rollback_payment"):
        print("rollback payment")
        session = Session()
        try:
            deduct_amt = quantity*10
            query = user_credit.update().where(user_credit.c.username == username).values(credit=user_credit.c.credit + deduct_amt)
            session.execute(query)
            session.commit()
            payment_rollback_counter.add(1)
            logger.info("Payment rollback committed", extra={"username": username, "quantity": quantity, "delivery": delivery})
        except Exception as e:
            logger.error("Error during database operation", exc_info=True)
        finally:
            session.close()
