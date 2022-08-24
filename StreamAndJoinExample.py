# Databricks notebook source
# MAGIC %run ./StreamJoin

# COMMAND ----------

# Enable auto compaction and optimized writes in Delta
spark.conf.set("spark.databricks.delta.optimizeWrite.enabled","true")
spark.conf.set("spark.databricks.delta.autoCompact.enabled","true")
spark.conf.set("spark.databricks.delta.properties.defaults.enableChangeDataFeed", True)

# COMMAND ----------

user = 'leon.eller@databricks.com'#dbutils.notebook.entry_point.getDbutils().notebook().getContext().tags().apply('user')

# COMMAND ----------

root_path = f"/Users/{user}/tmp/demo/cdc_raw"
customer_path = f"{root_path}/customers"
transaction_path = f"{root_path}/transactions"
orders_path = f"{root_path}/orders"
checkpointLocation = f"/Users/{user}/tmp/demo/cp"
schemaLocation = f"/Users/{user}/tmp/demo/schema"
# bronze_path = f"/Users/{user}/tmp/demo/bronze"
silver_path = f"/Users/{user}/tmp/demo/silver"
gold_path = f"/Users/{user}/tmp/demo/gold"

# COMMAND ----------

dbutils.fs.rm(checkpointLocation, True)
dbutils.fs.rm(silver_path, True)
dbutils.fs.rm(gold_path, True)

# COMMAND ----------

spark.sql(f'''
CREATE TABLE delta.`{silver_path}/transactions` ( amount STRING, customer_id STRING, id STRING, item_count STRING, operation STRING, operation_date STRING, transaction_date STRING) USING delta 
''')

# COMMAND ----------

spark.sql(f'''
CREATE TABLE delta.`{silver_path}/customers` ( address STRING, email STRING, firstname STRING, id STRING, lastname STRING, operation STRING, operation_date STRING)
USING delta
''')

# COMMAND ----------

spark.sql(f'''
CREATE TABLE delta.`{silver_path}/orders` ( delivery_date string,
id string,
item_name string,
operation string,
operation_date string,
transaction_id string) USING delta
''')

# COMMAND ----------

customer_stream = (
       spark.readStream.format("cloudFiles")
          .option('cloudFiles.format', 'json')
          .option('cloudFiles.schemaLocation', f'{schemaLocation}/customers')
          .option('cloudFiles.maxBytesPerTrigger', 20000)
          .load(customer_path)
          .drop('_rescued_data')
     )

# COMMAND ----------

transaction_stream = (
       spark.readStream.format("cloudFiles")
          .option('cloudFiles.format', 'json')
          .option('cloudFiles.schemaLocation', f'{schemaLocation}/transactions')
          .option('cloudFiles.maxBytesPerTrigger', 100)
          .load(transaction_path)
          .drop('_rescued_data')
     )

# COMMAND ----------

orders_stream = (
       spark.readStream.format("cloudFiles")
          .option('cloudFiles.format', 'json')
          .option('cloudFiles.schemaLocation', f'{schemaLocation}/orders')
          .option('cloudFiles.maxBytesPerTrigger', 2000000)
          .load(orders_path)
          .drop('_rescued_data')
     )

# COMMAND ----------

spark.sparkContext.setLocalProperty("spark.scheduler.pool", str(uuid.uuid4()))
(customer_stream
  .writeStream
  .format('delta')
  .option('checkpointLocation', f'{checkpointLocation}/silver/customers')
  .start(f'{silver_path}/customers'))

# COMMAND ----------

spark.sparkContext.setLocalProperty("spark.scheduler.pool", str(uuid.uuid4()))
(transaction_stream
  .writeStream
  .format('delta')
  .option('checkpointLocation', f'{checkpointLocation}/silver/transactions')
  .start(f'{silver_path}/transactions'))

# COMMAND ----------

spark.sparkContext.setLocalProperty("spark.scheduler.pool", str(uuid.uuid4()))
(orders_stream
  .writeStream
  .format('delta')
  .option('checkpointLocation', f'{checkpointLocation}/silver/orders')
  .start(f'{silver_path}/orders'))

# COMMAND ----------

def mergeGold(batchDf, batchId):
  deltaTable = DeltaTable.forPath(spark, f'{gold_path}/joined')
  deltaTable.alias("u").merge(
    source = batchDf.alias("staged_updates"),
    condition = F.expr("u.id = staged_updates.id AND u.customer_id = staged_updates.customer_id")) \
    .whenMatchedUpdateAll() \
    .whenNotMatchedInsertAll() \
    .execute()

# COMMAND ----------

a = (
      Stream.fromPath(f'{silver_path}/customers')
        .to(lambda df: df.withColumn('customer_id', df['id']).drop('id')) # drop duplicate id columns and rename customer's id to customer_id
        .to(lambda df: df.withColumn('customer_operation', df['operation']).drop('operation')) # drop duplicate operation columns and rename customer's oeration to customer_operation
        .to(lambda df: df.withColumn('customer_operation_date', df['operation_date']).drop('operation_date')) # drop duplicate operation_date columns and rename customer's operation_date to customer_operation_date
        .primaryKeys('customer_id')
        .sequenceBy('customer_operation_date')
    )

b = (
      Stream.fromPath(f'{silver_path}/transactions')
      .to(lambda df: df.withColumnRenamed('id', 'transaction_id'))
      .primaryKeys('transaction_id')
      .sequenceBy('operation_date')
    )

c = (
      Stream.fromPath(f'{silver_path}/orders')
            .to(lambda df: df.withColumnRenamed('id', 'order_id'))
            .to(lambda df: df.withColumnRenamed('operation', 'order_operation'))
            .to(lambda df: df.withColumnRenamed('operation_date', 'order_operation_date'))
            .primaryKeys('order_id')
            .sequenceBy('order_operation_date')
    )

j = (
  a.join(b)
   .on(lambda l, r: l['customer_id'] == r['customer_id'])
   .dedupJoinKeys('customer_id')
#  .onKeys('customer_id')
  .join(c)
  .on(lambda l, r: l['transaction_id'] == r['transaction_id'])
  .dedupJoinKeys('transaction_id')
#  .onKeys('transaction_id')
  .writeToPath(f'{gold_path}/joined')
#  .foreachBatch(mergeGold)
  .option("checkpointLocation", f'{checkpointLocation}/gold/joined')
  .queryName('gold')
  .start()
)

# COMMAND ----------

aa = spark.read.format('delta').load(f'{silver_path}/customers').withColumnRenamed('id', 'customer_id').withColumnRenamed('operation', 'customer_operation').withColumnRenamed('operation_date', 'customer_operation_date')
bb = spark.read.format('delta').load(f'{silver_path}/transactions').withColumnRenamed('id', 'transaction_id')
oo = spark.read.format('delta').load(f'{silver_path}/orders').withColumnRenamed('id', 'order_id').withColumnRenamed('operation', 'order_operation').withColumnRenamed('operation_date', 'order_operation_date')
bb_aa = bb.join(aa, bb['customer_id'] == aa['customer_id']).drop(aa['customer_id'])
cc = bb_aa.join(oo, oo['transaction_id'] == bb_aa['transaction_id']).drop(oo['transaction_id'])
cc.count()

# COMMAND ----------

df = spark.read.format('delta').load(f'{gold_path}/joined').select(cc.columns)
df.count()

# COMMAND ----------

display(df)

# COMMAND ----------

df_cols = df.columns
df_cols.sort()
cc_cols = cc.columns
cc_cols.sort()

# COMMAND ----------

print(df.select(df_cols).exceptAll(cc.select(cc_cols)).count())
print(cc.select(cc_cols).exceptAll(df.select(df_cols)).count())

# COMMAND ----------

display(cc.select(cc_cols).exceptAll(df.select(df_cols)))

# COMMAND ----------

display(df.select(df_cols).exceptAll(cc.select(cc_cols)))

# COMMAND ----------

display(df.select('transaction_id').exceptAll(cc.select('transaction_id')))

# COMMAND ----------

display(df.where("order_id = 'afefca93-beca-4f4b-b976-bacafcb24380'"))

# COMMAND ----------

display(cc.where("order_id = 'afefca93-beca-4f4b-b976-bacafcb24380'"))

# COMMAND ----------

display(df.where("transaction_id = '6433a31b-dbc6-4097-8fe7-653ee06fccd8'"))

# COMMAND ----------

display(cc.where("transaction_id = '6433a31b-dbc6-4097-8fe7-653ee06fccd8'"))

# COMMAND ----------

#%fs

#ls /Users/leon.eller@databricks.com/tmp/demo/both

# COMMAND ----------

# MAGIC %sql
# MAGIC 
# MAGIC --SELECT * FROM delta.`/Users/leon.eller@databricks.com/tmp/demo/newRight` WHERE transaction_id is null --ORDER BY ts
