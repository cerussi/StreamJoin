# StreamJoin

A framework for running incremental joins and aggregation over structured streaming CDF feeds from Delta tables.

An example in python:
```
  c = (
        Stream.fromPath(f'{silver_path}/customers')
          .primaryKeys('customer_id')
          .sequenceBy('customer_operation_date')
      )
  t = (
      Stream.fromPath(f'{silver_path}/transactions')
      .primaryKeys('transaction_id')
      .sequenceBy('operation_date')
    )

  j = (
    t.join(c, 'left')
    .onKeys('customer_id')
    .groupBy("customer_id")
    .agg(F.sum("amount").alias("total_amount"))
    .writeToPath(f'{gold_path}/aggs')
    .option("checkpointLocation", f'{checkpointLocation}/gold/aggs')
    .queryName(f'{gold_path}/aggs')
    .start()
  )
```
Unique primary keys (.primaryKeys()) are required per table for joins to ensure incremental merges have unique keys to merge on.
Sequence columns (.sequenceBy()) is required to ensure correct ordered processing/merging on rows from CDF.
