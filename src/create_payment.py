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

BROKER_URL = os.getenv("CELERY_BROKER_URL")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND")
celery_app = Celery('create_payment', broker=BROKER_URL,
                    backend=RESULT_BACKEND)
DATABASE_URL_PAYMENT = os.getenv("DATABASE_URL_PAYMENT")
engine = create_engine(DATABASE_URL_PAYMENT)
Session = sessionmaker(bind=engine)


# Let assume new user has 100 credit
# 1 late day cost 10 credit
@celery_app.task(name="create_payment")
def create_payment(payload: dict, fn: str):
    print("fn="+str(fn))
    print("payload="+str(payload))
    username: str = payload.get("username")
    quantity: int = payload.get("quantity")
    delivery: bool = payload.get("delivery")
    print("username="+str(username))
    print("quantity="+str(quantity))
    print("delivery="+str(delivery))
    print("db_url="+str(DATABASE_URL_PAYMENT))
    if fn == "pay":
        print("checking payment credit")
        session = Session()
        try:
            # query to check if username exist in db if not create new user with 100 credit
            query = select([user_credit]).where(user_credit.c.username == username)
            result = session.execute(query)
            print("result="+str(result))
            print("result.rowcount = "+str(result.rowcount))
            if result.rowcount == 0:
                print("create new user")
                query = insert(user_credit).values(username=username, credit=100)
                session.execute(query)
                session.commit()
            else:
                print("user already exist")

            # Query to check if user credit is enough to pay
            query = select([user_credit]).where(user_credit.c.username == username)
            credit_amt = session.execute(query).fetchone()  # Fetch the result row
            print("result_row=" + str(credit_amt))
            if credit_amt.credit < quantity*10:
                print("not enough credit")
                celery_app.send_task("create_order", queue='q01', args=[payload, "rollback_order"])
                return "INSUFFICIENT_FUND"                
            else:
                print("enough credit")
                commit_payment(username, quantity, delivery)
                print("payment committed successfully")
                inventory_task = celery_app.send_task("update_inventory", queue='q03', args=[payload, "update_inventory"])
                #returning result to the order service
                print("inventory_task_id="+str(inventory_task.id))
                return waiting_inventory_result(inventory_task.id)

        except Exception as e:
            print(f"Error during database operation: {e}")
        finally:
            session.close()
    elif fn == "rollback_payment":
        rollback_payment(username, quantity, delivery)
        celery_app.send_task("create_order", queue='q01', args=[payload, "rollback_order"])
        print("pasyment service send task to rollback order")
    else:
        print("invalid function name in payment service kub")

@celery_app.task
def waiting_inventory_result(inventory_task_id):
    time.sleep(0.2)
    inventory_task_result = AsyncResult(inventory_task_id)
    if inventory_task_result.ready():
        result_value = inventory_task_result.result
        print(f"Task result: {result_value}")
        return result_value
    else:
        print("inventory task is still running...")
        return "inventory task is still running..."
    
@celery_app.task
def commit_payment(username: str, quantity: int, delivery: bool):
    print("commiting payment")
    session = Session()
    try:
        deduct_amt = quantity*10
        query = user_credit.update().where(user_credit.c.username == username).values(credit=user_credit.c.credit - deduct_amt)
        session.execute(query)
        session.commit()
    except Exception as e:
        print(f"Error during database operation: {e}")
    finally:
        session.close()

@celery_app.task
def rollback_payment(username: str, quantity: int, delivery: bool):
    print("rollback payment")
    session = Session()
    try:
        deduct_amt = quantity*10
        query = user_credit.update().where(user_credit.c.username == username).values(credit=user_credit.c.credit + deduct_amt)
        session.execute(query)
        session.commit()
        print("payment rollback successfully")
    except Exception as e:
        print(f"Error during database operation: {e}")
    finally:
        session.close()
