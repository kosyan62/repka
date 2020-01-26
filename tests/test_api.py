import datetime as dt
import operator
from contextvars import ContextVar
from typing import Optional, List, Any, AsyncGenerator, Union

import pytest
import sqlalchemy as sa
from aiopg.sa import SAConnection, create_engine
from databases import Database
from pydantic import validator
from repka.api import BaseRepository, db_connection_var, ConnectionVarMixin
from repka.models import IdModel

# Enable async tests (https://github.com/pytest-dev/pytest-asyncio#pytestmarkasyncio)
pytestmark = pytest.mark.asyncio


class Transaction(IdModel):
    date: dt.date = None  # type: ignore
    price: int

    @validator("date", pre=True, always=True)
    def set_now_if_no_date(cls, value: Optional[dt.date]) -> dt.date:
        if value:
            return value

        return dt.datetime.now().date()


metadata = sa.MetaData()

transactions_table = sa.Table(
    "transactions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("date", sa.Date),
    sa.Column("price", sa.Integer),
)


class Task(IdModel):
    title: str
    priority = 0


tasks_table = sa.Table(
    "tasks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("title", sa.String),
    sa.Column("priority", sa.Integer, sa.Sequence("priority_seq", metadata=metadata)),
)


class TaskRepo(BaseRepository[Task]):
    table = tasks_table
    ignore_insert = ("priority",)


class TransactionRepo(BaseRepository[Transaction]):
    table = transactions_table

    async def sum(self) -> int:
        query = sa.select([sa.func.sum(transactions_table.c.price)])
        if isinstance(self.connection, SAConnection):
            sum_ = await self.connection.scalar(query)
        elif isinstance(self.connection, Database):
            sum_ = await self.connection.fetch_val(query)
        else:
            raise ValueError(f"Invalid connection type: {type(self.connection)}")
        return sum_


class TransactionRepoWithConnectionMixin(ConnectionVarMixin, BaseRepository[Transaction]):
    table = transactions_table

    def deserialize(self, **kwargs: Any) -> Transaction:
        return Transaction(**kwargs)


@pytest.fixture()
async def conn(db_url: str) -> AsyncGenerator[Union[SAConnection, Database], None]:
    # recreate all tables
    engine = sa.create_engine(db_url)
    metadata.drop_all(engine)
    metadata.create_all(engine)

    # create async connection
    if db_url.startswith("postgresql://"):
        async with create_engine(db_url) as engine:
            async with engine.acquire() as conn_:
                yield conn_
    elif db_url.startswith("sqlite://"):
        async with Database(db_url) as conn_:
            yield conn_


@pytest.fixture()
async def repo(conn: SAConnection) -> TransactionRepo:
    return TransactionRepo(conn)


@pytest.fixture()
async def task_repo(conn: SAConnection) -> TaskRepo:
    return TaskRepo(conn)


@pytest.fixture()
async def transactions(repo: TransactionRepo) -> List[Transaction]:
    transactions_ = [
        Transaction(price=100, date=dt.date(2019, 1, 3)),
        Transaction(price=200),
        Transaction(price=100, date=dt.date(2019, 1, 1)),
    ]
    transactions_ = await repo.insert_many(transactions_)
    return transactions_


async def test_base_repository_insert_sets_id_and_inserts_to_db(repo: TransactionRepo) -> None:
    trans = Transaction(price=100)

    trans = await repo.insert(trans)

    assert trans.id == 1

    db_trans = await repo.first()
    assert db_trans
    assert db_trans.id == trans.id


async def test_base_repo_insert_many_sets_ids(repo: TransactionRepo) -> None:
    transactions = [Transaction(price=100), Transaction(price=200)]

    transactions = await repo.insert_many(transactions)

    assert transactions[0].id == 1
    assert transactions[1].id == 2


async def test_base_repo_update_updates_row_in_db(repo: TransactionRepo) -> None:
    trans = Transaction(price=100)
    trans = await repo.insert(trans)
    trans.price = 300
    trans.date = dt.date(2019, 7, 1)

    await repo.update(trans)

    updated_trans = await repo.first()
    assert updated_trans
    assert updated_trans.price == trans.price
    assert updated_trans.date == trans.date


async def test_base_repo_update_partial_updates_some_fields(repo: TransactionRepo) -> None:
    old_price = 100
    old_date = dt.date(2019, 7, 1)
    trans = Transaction(price=old_price, date=old_date)
    trans = await repo.insert(trans)

    trans.price = 200
    new_date = dt.date(2019, 8, 1)
    await repo.update_partial(trans, date=new_date)

    updated_trans = await repo.first()
    assert updated_trans
    assert updated_trans.price == old_price
    assert updated_trans.date == new_date
    assert trans.date == new_date


async def test_base_repo_update_many(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    new_price = 300
    for trans in transactions:
        trans.price = new_price

    await repo.update_many(transactions)

    updated_trans = await repo.get_all()
    all(updated.price == new_price for updated in updated_trans)


async def test_base_repo_first_return_first_matching_row(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    trans = await repo.first(transactions_table.c.price == 100)

    assert trans
    assert trans.id == transactions[0].id


async def test_base_repo_get_all_return_all_rows_filtered_and_sorted(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    db_transactions = await repo.get_all(
        filters=[transactions_table.c.price == 100], orders=[transactions_table.c.date]
    )
    assert db_transactions == [transactions[2], transactions[0]]


async def test_base_repo_delete_deletes_row_from_db(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    await repo.delete(transactions_table.c.price == 100)

    db_transactions = await repo.get_all()
    assert len(db_transactions) == 1


async def test_transaction_repo_custom_method_works(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    sum_ = await repo.sum()

    assert sum_ == sum(map(operator.attrgetter("price"), transactions))


async def test_base_repo_get_by_id_returns_row_with_id(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    transaction_id = transactions[0].id
    assert transaction_id
    db_trans = await repo.get_by_id(transaction_id)
    assert db_trans == transactions[0]


async def test_base_repo_get_or_create_creates_entity_if_no_entities(
    repo: TransactionRepo
) -> None:
    price = 400
    trans, created = await repo.get_or_create(defaults={"price": price})
    assert created
    assert trans.price == price


async def test_base_repo_get_or_create_returns_entity_if_match(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    price = 400
    trans, created = await repo.get_or_create(
        filters=[transactions_table.c.id == transactions[0].id], defaults={"price": price}
    )
    assert not created
    assert trans == transactions[0]


async def test_get_by_ids_returns_multiple_objects(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    actual_transactions = await repo.get_by_ids([trans.id for trans in transactions if trans.id])
    assert actual_transactions == transactions


async def test_delete_by_id_deletes_object(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    trans_id = transactions[0].id
    assert trans_id

    await repo.delete_by_id(trans_id)
    assert not await repo.get_by_id(trans_id)


async def test_delete_by_ids_deletes_multiple_objects(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    await repo.delete_by_ids([trans.id for trans in transactions if trans.id])
    assert not await repo.get_all()


async def test_exists_returns_true_if_exists(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    assert await repo.exists(transactions_table.c.price == transactions[0].price)


async def test_exists_returns_false_if_not_exists(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    assert not await repo.exists(transactions_table.c.price + 9993 == transactions[0].price)


@pytest.mark.skip("No connection var support for SQLite")
async def test_connection_var_mixin_allows_to_create_repo_without_connection(
    conn: SAConnection
) -> None:
    trans = Transaction(price=100)

    db_connection_var.set(conn)
    repo = TransactionRepoWithConnectionMixin()
    trans = await repo.insert(trans)

    assert trans.id


@pytest.mark.skip("No connection var support for SQLite")
async def test_connection_var_mixin_allows_to_create_repo_without_connection_if_connection_var_is_third_party(
    conn: SAConnection
) -> None:
    trans = Transaction(price=100)

    new_db_connection_var: ContextVar[SAConnection] = ContextVar("new_db_connection_var")
    new_db_connection_var.set(conn)

    repo = TransactionRepoWithConnectionMixin(new_db_connection_var)
    trans = await repo.insert(trans)

    assert trans.id


async def test_first_returns_transaction_with_greatest_price(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    trans = await repo.first(orders=[-transactions_table.c.price])
    assert trans
    assert trans.price == max(trans.price for trans in transactions)


@pytest.mark.skip("No sequence support in sqlite")
async def test_insert_many_inserts_sequence_rows(task_repo: TaskRepo) -> None:
    tasks = [Task(title="task 1"), Task(title="task 2")]
    tasks = await task_repo.insert_many(tasks)
    assert tasks[0].priority == 1
    assert tasks[1].priority == 2


@pytest.mark.skip("No sequence support in sqlite")
async def test_insert_sets_ignored_column(task_repo: TaskRepo) -> None:
    task = Task(title="task 1", priority=1337)
    task = await task_repo.insert(task)
    assert task.priority == 1


async def test_get_all_ids(repo: TransactionRepo, transactions: List[Transaction]) -> None:
    ids = await repo.get_all_ids()
    assert ids == [trans.id for trans in transactions]


async def test_delete_without_args_raises_error(repo: TransactionRepo) -> None:
    with pytest.raises(ValueError):
        await repo.delete()


async def test_delete_with_none_deletes_all_entities(
    repo: TransactionRepo, transactions: List[Transaction]
) -> None:
    await repo.delete(None)
    assert (await repo.get_all()) == []
