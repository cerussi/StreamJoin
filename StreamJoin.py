# Databricks notebook source
from pyspark.sql import functions as F
from pyspark.sql.window import Window
import uuid
from  pyspark import StorageLevel
from functools import reduce
from pyspark.sql import Column
from delta.tables import *
import time
import os
import hashlib
import concurrent.futures
import itertools

# COMMAND ----------

import pyspark.sql.types
from pyspark.sql.types import _parse_datatype_string

def toDDL(self):
    """
    Returns a string containing the schema in DDL format.
    """
    from pyspark import SparkContext
    sc = SparkContext._active_spark_context
    dt = sc._jvm.__getattr__("org.apache.spark.sql.types.DataType$").__getattr__("MODULE$")
    json = self.json()
    return dt.fromJson(json).toDDL()
pyspark.sql.types.DataType.toDDL = toDDL
pyspark.sql.types.StructType.fromDDL = _parse_datatype_string

# COMMAND ----------

class StreamToStreamJoin:
  _left = None
  _right = None
  _joinType = None
  _dependentQuery = None
  _upstreamJoinCond = None

  def __init__(self,
               left,
               right,
               joinType):
    self._left = left
    self._right = right
    self._joinType = joinType
  
  def _chainStreamingQuery(self, dependentQuery, upstreamJoinCond):
    self._dependentQuery = dependentQuery
    self._upstreamJoinCond = upstreamJoinCond
    return self

  def stagingPath(self):
    return StreamToStreamJoinWithCondition(self._left,
               self._right,
               self._joinType,
               None,
               [])._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond).stagingPath()

  def on(self,
           joinExpr):
    if isinstance(joinExpr, Expression):
      joinExpr = joinExpr.toColumn(self._left, self._right)

    return StreamToStreamJoinWithCondition(self._left,
               self._right,
               self._joinType,
               joinExpr,
               None)._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond)
 
  def onKeys(self, *keys):
    joinExpr = lambda l, r: reduce(lambda c, e: c & e, [(l[k] == r[k]) for k in keys])
    def dropRight(f, l, r):
      for k in keys:
        f = f.drop(r[k])
      return f
    def dropLeft(f, l, r):
      for k in keys:
        f = f.drop(l[k])
      return f
    if self._joinType == 'right':
      func = dropLeft
    else:
      func = dropRight
    return StreamToStreamJoinWithCondition(self._left,
               self._right,
               self._joinType,
               joinExpr)._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond).to(func)

# COMMAND ----------

class GroupByWithAggs:
  _groupBy = None
  _aggCols = None
  _stream = None
  _partitionColumns = None
  _updateDict = None
  _dependentQuery = None
  _upstreamJoinCond = None

  def __init__(self, groupBy, aggCols, updateDict = None):
    self._groupBy = groupBy
    self._aggCols = aggCols
    self._updateDict = updateDict
    self._stream = groupBy.stream()

  def _chainStreamingQuery(self, dependentQuery, upstreamJoinCond):
    self._dependentQuery = dependentQuery
    self._upstreamJoinCond = upstreamJoinCond
    return self

  def stagingIndex(self):
    if self._dependentQuery is not None:
      return self._dependentQuery._depth(1)
    return 0

  def generateStagingName(self):
    name = f"$$_{'.'.join([str(c) for c in self._groupBy.columns()])}_{'.'.join([str(c) for c in self._aggCols])}"
    m = hashlib.sha256()
    m.update(name.encode('ascii'))
    m.update(self._stream.path().encode('ascii'))
    return f'{m.hexdigest()}_{self.stagingIndex()}'

  def generateStagingPath(self):
    dir = os.path.dirname(self._stream.path())
    return f'{dir}/{self.generateStagingName()}'

  def _doMerge(self, deltaTable, cond, updateCols, insertCols, keyCols, aggCols, deltaCalcs, batchDf, batchId):
    batchDf = batchDf.persist(StorageLevel.MEMORY_AND_DISK)
    plusDf = batchDf.where("_change_type != 'update_preimage'").groupBy(*self._groupBy.columns()).agg(*self._aggCols).alias("p")
    minusDf = batchDf.where("_change_type = 'update_preimage'").groupBy(*self._groupBy.columns()).agg(*self._aggCols).alias("m")
    batchDf = plusDf.join(minusDf, F.expr(" AND ".join([f"p.{k} <=> m.{k}" for k in keyCols])), how="left")
    batchDf = batchDf.select([f"p.{k}" for k in keyCols] + [deltaCalcs[ac] for ac in deltaCalcs])
    mergeChain = deltaTable.alias("u").merge(
        source = batchDf.alias("staged_updates"),
        condition = F.expr(cond))
    mergeChain.whenMatchedUpdate(set = updateCols) \
        .whenNotMatchedInsert(values = insertCols) \
        .execute()

  def _writeToTarget(self, deltaTableForFunc, tableName, path):
    schemaDf = self._stream.static().groupBy(*self._groupBy.columns()).agg(*self._aggCols)
    keyCols = schemaDf.columns[:len(self._groupBy.columns())]
    aggCols = schemaDf.columns[len(self._groupBy.columns()):]
    if self._updateDict is not None:
      schemaDf = schemaDf.alias("u").join(schemaDf.alias("staged_updates")).select([f"u.{c}" for c in keyCols + aggCols if c not in self._updateDict] + [(self._updateDict[k][1]).alias(k) for k in self._updateDict])
    ddl = schemaDf.schema.toDDL()
    createSql = f'CREATE TABLE IF NOT EXISTS {tableName}({ddl}) USING DELTA'
    if path is not None:
      createSql = f"{createSql} LOCATION '{path}'"
    if self._partitionColumns is not None:
      createSql = f"{createSql} PARTITIONED BY ({', '.join([pc.column() for pc in self._partitionColumns])})"
    spark.sql(createSql)
    cond = " AND ".join([f"u.{kc} <=> staged_updates.{kc}" for kc in keyCols])
    deltaCalcs = {ac: F.expr(f"CASE WHEN m.{ac} is not null THEN p.{ac} - m.{ac} ELSE p.{ac} END as {ac}") for ac in aggCols}
    updateCols = {ac: F.col(f'u.{ac}') + F.col(f'staged_updates.{ac}') for ac in aggCols}
    insertCols = {ic: F.col(f'staged_updates.{ic}') for ic in (keyCols + aggCols)}
    if self._updateDict is not None:
      for k in self._updateDict:
        updateCols[k] = self._updateDict[k][1]
        insertCols[k] = self._updateDict[k][0]
        deltaCalcs[k] = F.expr(f"CASE WHEN m.{k} is not null THEN {self._updateDict[k][2]} ELSE p.{k} END as {k}")
    def mergeFunc(batchDf, batchId):
      batchDf._jdf.sparkSession().conf().set('spark.databricks.optimizer.adaptive.enabled', True)
      batchDf._jdf.sparkSession().conf().set('spark.sql.adaptive.forceApply', True)
      deltaTable = deltaTableForFunc()
      self._doMerge(deltaTable, cond, updateCols, insertCols, keyCols, aggCols, deltaCalcs, batchDf, batchId)
    return DataStreamWriter(
      (
        self._stream.stream().writeStream.foreachBatch(mergeFunc)
      )
    )._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond)

  def partitionBy(self, *columns):
    self._partitionColumns = [(c if isinstance(c, PartitionColumn) else PartitionColumn(c)) for c in columns]
    return self

  def reduce(self, column, update, delta_update = None, insert = None):
    if insert is None:
      insert = F.col(f"staged_updates.{column}")
    if delta_update is None:
      delta_update = f"p.{column} - m.{column}"
    if update is None:
      update = F.col(f'u.{column}') + F.col(f'staged_updates.{column}')
    if self._updateDict is None:
      self._updateDict = {}
    self._updateDict[column] = (insert, update, delta_update)
    return self

  def join(self, right, joinType = 'inner', stagingPath = None):
    if stagingPath is None:
      stagingPath = self.generateStagingPath()
    query = (
                  self.writeToPath(f'{stagingPath}/data')
                      .option('checkpointLocation', f'{stagingPath}/cp')
                      .queryName(self.generateStagingName())
                )
    return ( Stream.fromPath(f'{stagingPath}/data').setName(self.generateStagingName()).primaryKeys(*self._groupBy.columns())
               .join(right, joinType)
               ._chainStreamingQuery(query, None) )
  

  def groupBy(self, *cols, stagingPath = None):
    if stagingPath is None:
      stagingPath = self.generateStagingPath()
    query = (
                  self.writeToPath(f'{stagingPath}/data')
                      .option('checkpointLocation', f'{stagingPath}/cp')
                      .queryName(self.generateStagingName())
                )
    return ( Stream.fromPath(f'{stagingPath}/data').setName(self.generateStagingName()).primaryKeys(*self._groupBy.columns())
               .groupBy(*cols)
               ._chainStreamingQuery(query, None) )

  def writeToPath(self, path):
      return self._writeToTarget(lambda: DeltaTable.forPath(spark, path), f'delta.`{path}`', path)

  def writeToTable(self, tableName):
    return self._writeToTarget(lambda: DeltaTable.forName(spark, tableName), tableName, None)

# COMMAND ----------

class GroupBy:
  _cols = None
  _stream = None
  _dependentQuery = None
  _upstreamJoinCond = None

  def __init__(self, stream, cols):
    self._cols = cols
    self._stream = stream

  def _chainStreamingQuery(self, dependentQuery, upstreamJoinCond):
    self._dependentQuery = dependentQuery
    self._upstreamJoinCond = upstreamJoinCond
    return self

  def agg(self, *aggCols):
    return GroupByWithAggs(self, aggCols)._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond)
  
  def stream(self):
    return self._stream
  
  def columns(self):
    return self._cols

# COMMAND ----------

class Expression:
  _left = None
  _right = None
  _func = None
  
  def __init__(self,
               left,
               right,
               func = None):
    self._left = left
    self._right = right
    self._func = func

  def toColumn(self, left, right):
    func = self._func
    return self._toColumnFunc(left, right, lambda le, re: lambda l, r: func(le(l, r), re(l, r)))

  def _toColumnFunc(self, left, right, func):
    leftExpr = self._left.toColumn(left, right)
    rightExpr = self._right.toColumn(left, right)
    return func(leftExpr, rightExpr)

  def _create(self, other, func):
    if isinstance(other, ColumnSelector):
      return func(self, ColumnRef(other))
    elif isinstance(other, Expression):
      return func(self, other)
    elif isinstance(other, Column):
      return func(self, ColumnRef(other))
    else:
      raise Exception(f'Expression of type {type(other)} is not supported')
    
  def __eq__(self, other):
    return self._create(other, lambda l, r: Expression(l, r, lambda a, b: a == b))
  def __lt__(self, other):
    return self._create(other, lambda l, r: Expression(l, r, lambda a, b: a < b))
  def __gt__(self, other):
    return self._create(other, lambda l, r: Expression(l, r, lambda a, b: a > b))
  def __le__(self, other):
    return self._create(other, lambda l, r: Expression(l, r, lambda a, b: a <= b))
  def __ge__(self, other):
    return self._create(other, lambda l, r: Expression(l, r, lambda a, b: a >= b))
  def __ne__(self, other):
    return self._create(other, lambda l, r: Expression(l, r, lambda a, b: a != b))

  def __and__(self, other):
    return self._create(other, lambda l, r: Expression(l, r, lambda a, b: a & b))

  def __or__(self, other):
    return self._create(other, lambda l, r: Expression(l, r, lambda a, b: a | b))

class ColumnRef(Expression):
  def __init__(self, ref):
    super().__init__(ref, ref)

  def toColumn(self, left, right):
    if isinstance(self._right, ColumnSelector):
      if self._right.frame() is right:
        columnName = self._right.columnName()
        return lambda l, r: r[columnName]
      columnName = self._right.columnName()
      return lambda l, r: l[columnName]
    else:
      col = self._right
      return lambda l, r: col

# COMMAND ----------

class ColumnSelector:
  _stream = None
  _columnName = None
  _func = None

  def __init__(self,
               stream,
               columnName):
    self._stream = stream
    self._columnName = columnName

  def frame(self):
    return self._stream

  def stream(self):
    return self._stream.stream()

  def columnName(self):
    return self._columnName
  
  def transform(self, col):
    if self._func is None:
      return col
    return self._func(col)

  def to(self, func):
    self._func = func
    return self
  
  def __eq__(self, other):
    return ColumnRef(self) == other
  def __lt__(self, other):
    return ColumnRef(self) < other
  def __gt__(self, other):
    return ColumnRef(self) > other
  def __le__(self, other):
    return ColumnRef(self) <= other
  def __ge__(self, other):
    return ColumnRef(self) >= other
  def __ne__(self, other):
    return ColumnRef(self) != other

  def __and__(self, other):
    return ColumnRef(self) & other

# COMMAND ----------

class Stream:
  _stream = None
  _staticReader = None
  _static = None
  _primaryKeys = None
  _sequenceColumns = None
  _path = None
  _name = None
  _isTable = None
  excludedColumns = ['_commit_version', '_change_type']

  def __init__(self,
               stream,
               staticReader,
               isTable):
    self._stream = stream
    self._staticReader = staticReader
    self._isTable = isTable
  
  @staticmethod
  def readAtVersion(reader, version = None):
    if version is not None:
      loader = reader.option('versionAsOf', version)
    else:
      loader = reader
    return loader
    
  @staticmethod
  def fromPath(path, startingVersion = None):
    cdfStream = spark.readStream.format('delta').option("readChangeFeed", "true").option("maxBytesPerTrigger", "1g")
    if startingVersion is not None:
      cdfStream.option("startingVersion", "{startingVersion}")
    cdfStream = cdfStream.load(path)
    cdfStream = cdfStream.where("_change_type != 'delete'").drop('_commit_timestamp')
    reader = spark.read.format('delta')
    return Stream(cdfStream, lambda v: Stream.readAtVersion(reader, v).load(path), False).setPath(path)

  @staticmethod
  def fromTable(tableName, startingVersion = None):
    cdfStream = spark.readStream.format('delta').option("readChangeFeed", "true").option("maxBytesPerTrigger", "1g")
    if startingVersion is not None:
      cdfStream.option("startingVersion", "{startingVersion}")
    cdfStream = cdfStream.table(tableName)
    cdfStream = cdfStream.where("_change_type != 'delete'").drop('_commit_timestamp')
    reader = spark.read.format('delta')
    return Stream(cdfStream, lambda v: Stream.readAtVersion(reader, v).table(tableName), True).setName(tableName).setPath(tableName)

  def __getitem__(self, key):
    return ColumnSelector(self, key)
  
  def setName(self, name):
    self._name = name
    return self

  def name(self):
    if self._name is None or len(self._name) == 0:
      self._name = os.path.basename(self.path())
    return self._name

  def setPath(self, path):
    self._path = path
    return self
  
  def path(self):
    return self._path

  def columns(self):
    return [c for c in self._stream.columns if c not in Stream.excludedColumns]

  def stream(self):
    return self._stream

  def static(self, version = None):
    if version is None:
      if self._static is None:
        self._static = self._staticReader(version)
      return self._static
    return self._staticReader(version)

  def getLatestVersion(self):
    if self._isTable is True:
      return DeltaTable.forName(spark, self.name()).history(1).select('version').collect()[0][0]
    return DeltaTable.forPath(spark, self.path()).history(1).select('version').collect()[0][0]

  def primaryKeys(self, *keys):
    self._primaryKeys = keys
    return self
  
  def getPrimaryKeys(self):
    return self._primaryKeys

  def sequenceBy(self, *columns):
    self._sequenceColumns = columns
    return self
  
  def getSequenceColumns(self):
    return self._sequenceColumns

  def join(self, right, joinType = 'inner'):
    return StreamToStreamJoin(self, right, joinType)
  
  def groupBy(self, *cols):
    return GroupBy(self, cols)
  
  def to(self, func):
    self._stream = func(self._stream)
#     self._static = func(self._static)
    reader = self._staticReader
    self._staticReader = lambda v: func(reader(v))
    return self

# COMMAND ----------

class PartitionColumn:
  _column = None
  _staticPruned = False

  def __init__(self,
               column):
    if isinstance(column, prune):
      self._column = column.column()
      self._staticPruned = True
    else:
      self._column = column
      self._staticPruned = False
  
  def column(self):
    return self._column
  
  def isStaticPruned(self):
    return self._staticPruned

# COMMAND ----------

class prune:
  _column = None

  def __init__(self,
               column):
    self._column = column

  def column(self):
    return self._column

# COMMAND ----------

class MicrobatchJoin:
  _leftMicrobatch = None
  _leftStatic = None
  _rightMicrobatch = None
  _rightStatic = None
  _persisted = []

  def __init__(self,
               leftMicrobatch,
               leftStatic,
               rightMicrobatch,
               rightStatic):
    self._leftMicrobatch = leftMicrobatch
    self._leftStatic = leftStatic
    self._rightMicrobatch = rightMicrobatch
    self._rightStatic = rightStatic
  
  @staticmethod
  def _transform(func, f, l, r):
    if func is not None:
      return func(f, l, r)
    return f

  def join(self,
           joinType,
           joinExpr,
           primaryKeys,
           transformFunc,
           selectCols,
           finalSelectCols):
    if isinstance(selectCols, tuple) or isinstance(selectCols, str):
      dropDupKeys = MicrobatchJoin._transform
      selectFunc = lambda f, l, r: f.selectExpr(*selectCols)
      finalSelectFunc = lambda f, l, r: f.selectExpr(*selectCols)
    else:
      dropDupKeys = lambda func, f, l, r: f
      selectFunc = lambda f, l, r: f.select(*selectCols(l, r))
      finalSelectFunc = lambda f, l, r: f.select(*finalSelectCols(l, r))

    newLeft = F.broadcast(self._leftMicrobatch).join(self._rightStatic, joinExpr(self._leftMicrobatch, self._rightStatic), 'left' if joinType == 'left' else 'inner')
    newLeft = dropDupKeys(transformFunc, newLeft, self._leftMicrobatch, self._rightStatic)
    newLeft = selectFunc(newLeft, self._leftMicrobatch, self._rightStatic)

    newRight = F.broadcast(self._rightMicrobatch).join(self._leftStatic, joinExpr(self._leftStatic, self._rightMicrobatch), 'left' if joinType == 'right' else 'inner')
    newRight = dropDupKeys(transformFunc, newRight, self._leftStatic, self._rightMicrobatch)
    newRight = selectFunc(newRight, self._leftStatic, self._rightMicrobatch)

    primaryJoinExpr = reduce(lambda e, pk: e & pk, [newLeft[k].eqNullSafe(newRight[k]) for k in primaryKeys])

    joinedOuter = newLeft.join(newRight, joinExpr(newLeft, newRight) & primaryJoinExpr, 'outer').persist(StorageLevel.MEMORY_AND_DISK)
    self._persisted.append(joinedOuter)
    if joinType == 'inner' or joinType == 'right' or joinType == 'left':
      left = joinedOuter.where(reduce(lambda e, pk: e & pk, [newRight[pk].isNull() for pk in primaryKeys])).select(newLeft['*'])
      right = joinedOuter.where(reduce(lambda e, pk: e & pk, [newLeft[pk].isNull() for pk in primaryKeys])).select(newRight['*'])
    else:
      raise Exception(f'{joinType} join type is not supported')
    if joinType == 'inner':
      filter = [(newLeft[pk].isNotNull() & newRight[pk].isNotNull()) for pk in primaryKeys]
    else:
      filter = [((newLeft[pk].isNotNull() & newRight[pk].isNotNull()) | (newLeft[pk].isNull() & newRight[pk].isNull())) for pk in primaryKeys]
    both = joinedOuter.where(reduce(lambda e, pk: e & pk, filter))
    both = dropDupKeys(transformFunc, both, newLeft, newRight)
    both = selectFunc(both, newLeft, newRight)
    unionDf = left.unionByName(right).unionByName(both)
    unionDf = unionDf.where(reduce(lambda e, pk: e | pk, [unionDf[pk].isNotNull() for pk in primaryKeys]))
    finalDf = finalSelectFunc(unionDf, unionDf, unionDf)
    finalDf = finalDf.persist(StorageLevel.MEMORY_AND_DISK)
    self._persisted.append(finalDf)
    return finalDf
  
  def __enter__(self):
    return self
  
  def __exit__(self, exc_type, exc_value, traceback):
    for df in self._persisted:
      df.unpersist()
    self._persisted.clear()

# COMMAND ----------

class StreamingQuery:
  _streamingQuery = None
  _dependentQuery = None

  def __init__(self,
               streamingQuery,
               dependentQuery):
    self._streamingQuery = streamingQuery
    self._dependentQuery = dependentQuery
  
  @property
  def lastProgress(self):
    pdict = {}
    if self._dependentQuery is not None:
      pdict.update(self._dependentQuery.lastProgress)
    pdict[self._streamingQuery.name] = self._streamingQuery.lastProgress
    return pdict

  @property
  def recentProgress(self):
    pdict = {}
    if self._dependentQuery is not None:
      pdict.update(self._dependentQuery.recentProgress)
    pdict[self._streamingQuery.name] = self._streamingQuery.recentProgress
    return pdict

  @property
  def isActive(self):
    if self._dependentQuery is not None:
      if self._dependentQuery.isActive is True:
        return True
    return self._streamingQuery.isActive

  def awaitTermination(self, timeout=None):
    if self._dependentQuery is not None:
      self._dependentQuery.awaitTermination(timeout)
    return self._streamingQuery.awaitTermination(timeout)

  def stop(self):
    if self._dependentQuery is not None:
      self._dependentQuery.stop()
    return self._streamingQuery.stop()
  
  def awaitAllProcessed(self, maxConsecutiveNoBytesOutstandingMicrobatchRetries = 6):
    lastBatches = {}
    testTryCount = 0
    while(True):
      lp = self.lastProgress
      sources = [lp[k]['sources'][0] for k in lp if lp[k] is not None]
      if len(sources) == len(lp):
        bytes = [int(s['metrics']['numBytesOutstanding']) if s.get('metrics') is not None else 1 for s in sources]
        batches = {k: lp[k]['timestamp'] for k in lp}
        updatedBatches = [batches[bi] for bi in batches if bi in lastBatches and batches[bi] != lastBatches[bi]]
        if sum(bytes) == 0:
          if len(updatedBatches) > 0:
            if testTryCount >= maxConsecutiveNoBytesOutstandingMicrobatchRetries:
              break
            else:
              testTryCount += 1
        else:
          testTryCount = 0
      self.awaitTermination(5)
      lastBatches.update(batches)

  def awaitAllProcessedAndStop(self):
    self.awaitAllProcessed()
    self.stop()

# COMMAND ----------

class DataStreamWriter:
  _streamingQuery = None
  _dependentQuery = None
  _upstreamJoinCond = None

  def __init__(self,
               streamingQuery):
    self._streamingQuery = streamingQuery
  
  def _chainStreamingQuery(self, dependentQuery, upstreamJoinCond):
    self._dependentQuery = dependentQuery
    self._upstreamJoinCond = upstreamJoinCond
    return self

  def _depth(self, index):
    if self._dependentQuery is not None:
      return self._dependentQuery._depth(index + 1)
    return index
    
  def option(self, name, value):
    self._streamingQuery = self._streamingQuery.option(name, value)
    return self
    
  def trigger(self, availableNow=None, processingTime=None, once=None, continuous=None):
    if self._dependentQuery is not None:
      self._dependentQuery.trigger(availableNow=availableNow, processingTime=processingTime, once=once, continuous=continuous)
    self._streamingQuery = self._streamingQuery.trigger(availableNow=availableNow, processingTime=processingTime, once=once, continuous=continuous)
    return self
  
  def queryName(self, name):
    self._streamingQuery = self._streamingQuery.queryName(name)
    return self
  
  @property
  def stream(self):
    return self._streamingQuery

  def start(self):
    dq = None
    if self._dependentQuery is not None:
      dq = self._dependentQuery.start()
    spark.sparkContext.setLocalProperty("spark.scheduler.pool", str(uuid.uuid4()))
    sq = self.stream.start()
    return StreamingQuery(sq, dq)

class StreamingJoin:
  _left = None
  _right = None
  _joinType = None
  _mergeFunc = None
  _dependentQuery = None
  _upstreamJoinCond = None

  def __init__(self,
               left,
               right,
               joinType,
               mergeFunc):
    self._left = left
    self._right = right
    self._joinType = joinType
    self._mergeFunc = mergeFunc
    self._primaryKeys = list(dict.fromkeys(self._left.getPrimaryKeys() + self._right.getPrimaryKeys()))

  def _chainStreamingQuery(self, dependentQuery, upstreamJoinCond):
    self._dependentQuery = dependentQuery
    self._upstreamJoinCond = upstreamJoinCond
    return self

  def _merge(self,
             joinExpr,
             transformFunc,
             selectCols,
             finalSelectCols):
    leftStatic = self._left.static()
    rightStatic = self._right.static()
    mergeFunc = self._mergeFunc
    lastLeftMaxCommitVersion = None
    lastRightMaxCommitVersion = None
    def _mergeJoin(batchDf, batchId):
      nonlocal lastLeftMaxCommitVersion
      nonlocal lastRightMaxCommitVersion
      left = batchDf.where("left is not null AND left._change_type != 'update_preimage'").select('left.*')
      right = batchDf.where("right is not null AND right._change_type != 'update_preimage'").select('right.*')
      maxCommitVersions = (
                                left.agg(F.max('_commit_version').alias('_left_commit_version'), F.lit(None).alias('_right_commit_version'))
                                    .unionByName(right.agg(F.lit(None).alias('_left_commit_version'), F.max('_commit_version').alias('_right_commit_version')))
                                    .agg(F.sum('_left_commit_version').alias('_left_commit_version'), F.sum('_right_commit_version').alias('_right_commit_version'))
                                    .collect()[0]
                             )
      # We want to grab the max commit version in the microbatch so we do a consistent read of left and right static pinned at that version
      # otherwise the read may be non-deterministic due to lazy spark evaluation
      leftMaxCommitVersion = maxCommitVersions[0]
      rightMaxCommitVersion = maxCommitVersions[1]
      leftStaticLocal = leftStatic
      rightStaticLocal = rightStatic
      if leftMaxCommitVersion is None:
        leftMaxCommitVersion = lastLeftMaxCommitVersion
      if rightMaxCommitVersion is None:
        rightMaxCommitVersion = lastRightMaxCommitVersion
      if leftMaxCommitVersion is None:
        leftMaxCommitVersion = self._left.getLatestVersion()
      if rightMaxCommitVersion is None:
        rightMaxCommitVersion = self._right.getLatestVersion()
      if leftMaxCommitVersion is not None:
        leftStaticLocal = self._left.static(leftMaxCommitVersion)
      if rightMaxCommitVersion is not None:
        rightStaticLocal = self._right.static(rightMaxCommitVersion)
      lastLeftMaxCommitVersion = leftMaxCommitVersion
      lastRightMaxCommitVersion = rightMaxCommitVersion
      with MicrobatchJoin(left, leftStaticLocal, right, rightStaticLocal) as mj:
        joinedBatchDf = mj.join(self._joinType,
                                joinExpr,
                                self._primaryKeys,
                                transformFunc,
                                selectCols,
                                finalSelectCols)
        return mergeFunc(joinedBatchDf, batchId)
    return _mergeJoin

  def join(self,
           joinExpr,
           transformFunc,
           selectCols,
           finalSelectCols):
    packed = self._left.stream().select(F.struct('*').alias('left'), F.lit(None).alias('right')).unionByName(self._right.stream().select(F.lit(None).alias('left'), F.struct('*').alias('right')))
    return DataStreamWriter(
      (packed
        .writeStream 
        .foreachBatch(self._merge(joinExpr, transformFunc, selectCols, finalSelectCols))
      )
    )._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond)

# COMMAND ----------

class StreamToStreamJoinWithConditionForEachBatch:
  _left = None
  _right = None
  _joinType = None
  _joinExpr = None
  _transformFunc = None
  _partitionColumns = None
  _selectCols = None
  _finalSelectCols = None
  _dependentQuery = None
  _upstreamJoinCond = None

  def __init__(self,
               left,
               right,
               joinType,
               onCondition,
               transformFunc,
               partitionColumns,
               selectCols,
               finalSelectCols):
    self._left = left
    self._right = right
    self._joinType = joinType
    self._joinExpr = onCondition
    self._transformFunc = transformFunc
    self._partitionColumns = partitionColumns
    self._selectCols = selectCols
    self._finalSelectCols = finalSelectCols
  
  def _chainStreamingQuery(self, dependentQuery, upstreamJoinCond):
    self._dependentQuery = dependentQuery
    self._upstreamJoinCond = upstreamJoinCond
    return self

  def _safeMergeLists(self, l, r):
    a = l
    if r is not None:
      if a is None:
        a = r
      else:
        a = a + r
    if a is not None:
      a = list(dict.fromkeys(a))
    return a

  def partitionBy(self, *columns):
    self._partitionColumns = [(c if isinstance(c, PartitionColumn) else PartitionColumn(c)) for c in columns]
    return self

  def foreachBatch(self, mergeFunc):
    windowSpec = None
    primaryKeys = self._safeMergeLists(self._left.getPrimaryKeys(), self._right.getPrimaryKeys())
    sequenceColumns = self._safeMergeLists(self._left.getSequenceColumns(), self._right.getSequenceColumns())
    if primaryKeys is not None and len(primaryKeys) > 0 and sequenceColumns is not None and len(sequenceColumns) > 0:
      windowSpec = Window.partitionBy(primaryKeys).orderBy([F.desc(sc) for sc in sequenceColumns])
    def mergeTransformFunc(batchDf, batchId):
      batchDf = batchDf.where("_change_type != 'update_preimage'")
      return mergeFunc(self._dedupBatch(batchDf, windowSpec, primaryKeys), batchId)
    return StreamingJoin(self._left,
               self._right,
               self._joinType,
               mergeTransformFunc).join(self._joinExpr,
                               self._transformFunc,
                               self._selectCols,
                               self._finalSelectCols)._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond)

  def _dedupBatch(self, batchDf, windowSpec, primaryKeys):
    if windowSpec is not None:
      batchDf = batchDf.withColumn('__row_number', F.row_number().over(windowSpec)).where('__row_number = 1')
    else:
      batchDf = batchDf.dropDuplicates(primaryKeys)
    return batchDf

  def _doMerge(self, deltaTable, cond, primaryKeys, sequenceWindowSpec, updateCols, matchCondition, batchDf, batchId):
#    print(f'****** {cond} ******')
    mergeChain = deltaTable.alias("u").merge(
        source = batchDf.alias("staged_updates"),
        condition = F.expr(cond))
    mergeChain.whenMatchedUpdate(condition = matchCondition, set = updateCols) \
        .whenNotMatchedInsert(values = updateCols) \
        .execute()

  def _mergeCondition(self, nonNullableKeys, nullableKeys, extraCond = ''):
    arr = []
    for i in range(0, len(nullableKeys)+1):
      nullKeys = nullableKeys.copy()
      t = list(itertools.combinations(nullKeys, i))
      for ii in range(0, len(t)):
        item = nonNullableKeys.copy()
        out = [f'u.{pk} = staged_updates.{pk}' for pk in item]
        for iii in range(0, len(t[ii])):
          item += [t[ii][iii]]
          out += [f'u.{t[ii][iii]} = staged_updates.{t[ii][iii]}']
        hasNullable = False
        for pk in nullKeys:
          if pk not in item:
            out += [f'(u.{pk} is null OR staged_updates.{pk} is null)']
            hasNullable = True
        arr += [f"({' AND '.join(out)}{extraCond if len(nullableKeys) > 0 else ''})"]
    return ' OR '.join(arr)

  def _mergeNonNullKeysForJoin(self, joinType, nonNullKeys, nullKeys, nonNullCandidateKeys, nullCandidateKeys):
    if joinType == 'inner':
      return list(dict.fromkeys(nonNullKeys + [pk for pk in nonNullCandidateKeys if pk not in nullKeys]))
    elif joinType == 'left':
      return list(dict.fromkeys([pk for pk in nonNullKeys if pk not in nullCandidateKeys] + [pk for pk in nonNullCandidateKeys if pk not in nullKeys]))
    elif joinType == 'right':
      return list(dict.fromkeys([pk for pk in nonNullCandidateKeys if pk not in nullKeys] + [pk for pk in nonNullKeys if pk not in nullCandidateKeys]))

  def _mergeNullKeysForJoin(self, joinType, nonNullKeys, nullKeys, nonNullCandidateKeys, nullCandidateKeys):
    if joinType == 'inner':
      return list(dict.fromkeys(nullKeys + [pk for pk in nullCandidateKeys if pk not in nonNullKeys and pk not in nullKeys and pk not in nonNullCandidateKeys]))
    elif joinType == 'left':
      return list(dict.fromkeys([pk for pk in nullKeys if pk not in nonNullKeys] + [pk for pk in nullCandidateKeys if pk not in nonNullKeys]))
    elif joinType == 'right':
      return list(dict.fromkeys([pk for pk in nullKeys if pk not in nonNullKeys] + [pk for pk in nullCandidateKeys if pk not in nullKeys]))

  def _buildPrunedPartitionColumnFunc(self, prunedPartitionColumns, partitionColumnsExpr, hasNullableKeys):
    partitionColumnsExprFunc = None
    if len(prunedPartitionColumns) > 0:
      def pruneFunc(batchDf):
        colValues = [batchDf.select(pc.column()).distinct().collect() for pc in prunedPartitionColumns]
        colValues = [[f"'{v[0]}'" if isinstance(v[0], str) else str(v[0]) if v[0] is not None else 'null' for v in vals] for vals in colValues]
        pcAndVals = [(pc, vals) for pc, vals in zip(prunedPartitionColumns, colValues)]
        expr = ' AND '.join([f"(u.{pc[0].column()} in ({','.join(pc[1])})" + (f' OR u.{pc[0].column()} is null' if hasNullableKeys else '') + ')' for pc in pcAndVals if len(pc[1]) > 0])
        return expr
      if partitionColumnsExpr is not None and len(partitionColumnsExpr) > 0:
        partitionColumnsExprFunc = lambda batchDf: f'{partitionColumnsExpr} AND {pruneFunc(batchDf)}'
      else:
        partitionColumnsExprFunc = pruneFunc
    return partitionColumnsExprFunc

  def _writeToTarget(self, deltaTableForFunc, tableName, path):
    leftStatic = self._left.static()
    rightStatic = self._right.static()
    schemaDf = leftStatic.join(rightStatic,
                                                   self._joinExpr(leftStatic, rightStatic))
    if self._transformFunc is not None:
      schemaDf = self._transformFunc(schemaDf, leftStatic, rightStatic)
    schemaDf = schemaDf.select(self._finalSelectCols(leftStatic, rightStatic))
    ddl = schemaDf.schema.toDDL()
    createSql = f'CREATE TABLE IF NOT EXISTS {tableName}({ddl}) USING DELTA'
    if path is not None:
      createSql = f"{createSql} LOCATION '{path}'"
    if self._partitionColumns is not None:
      createSql = f"{createSql} PARTITIONED BY ({', '.join([pc.column() for pc in self._partitionColumns])})"
    spark.sql(createSql)

    primaryKeys = self._safeMergeLists(self._left.getPrimaryKeys(), self._right.getPrimaryKeys())
    sequenceColumns = self._safeMergeLists(self._left.getSequenceColumns(), self._right.getSequenceColumns())
    pks = [[], []]
    if self._upstreamJoinCond is not None:
      pks = self._upstreamJoinCond()
#       print(f'%%%%%%%%%% nonNullKeys = {pks[0]}')
#       print(f'%%%%%%%%%% nullKeys = {pks[1]}')
    pks1 = self._nonNullAndNullPrimaryKeys(self._joinType,
                                           [pk for pk in primaryKeys if pk in self._left.getPrimaryKeys()],
                                           [pk for pk in primaryKeys if pk in self._right.getPrimaryKeys()])
#     print(f'%%%%%%%%%% self._joinType = {self._joinType}')
#     print(f'%%%%%%%%%% nonNullCandidateKeys = {pks1[0]}')
#     print(f'%%%%%%%%%% nullCandidateKeys = {pks1[1]}')
    pks = [self._mergeNonNullKeysForJoin(self._joinType, pks[0], pks[1], pks1[0], pks1[1]), self._mergeNullKeysForJoin(self._joinType, pks[0], pks[1], pks1[0], pks1[1])]
#     print(f'%%%%%%%%%% pks[0] = {pks[0]}')
#     print(f'%%%%%%%%%% pks[1] = {pks[1]}')
    condInitial = ' AND '.join([f'u.{pk} = staged_updates.{pk}' for pk in pks[0]] + [f'u.{pk} <=> staged_updates.{pk}' for pk in pks[1]])
    partitionColumns = []
    prunedPartitionColumns = []
    partitionColumnsExprFunc = None
    if self._partitionColumns is not None and len(self._partitionColumns) > 0:
      partitionColumns = list(self._partitionColumns)
      prunedPartitionColumns = [pc for pc in partitionColumns if pc.isStaticPruned()]
      partitionColumnsExpr = ' AND '.join([f'(u.{pc.column()} <=> staged_updates.{pc.column()}' + (f' OR u.{pc.column()} is null' if len(pks[1]) > 0 else '') + ')' for pc in partitionColumns if not pc.isStaticPruned()])
      partitionColumnsExprFunc = self._buildPrunedPartitionColumnFunc(prunedPartitionColumns, partitionColumnsExpr, len(pks[1]) > 0)
      if partitionColumnsExprFunc is None:
        condInitial = f'({partitionColumnsExpr}) AND ({condInitial})'
    outerCondInitial = None
    dedupWindowSpec = None
    outerWindowSpec = None
    matchCondition = None
    insertFilter = None
    updateFilter = None
    deltaTableColumns = deltaTableForFunc().toDF().columns
    if len(pks[1]) > 0:
      outerCondStr = self._mergeCondition(pks[0], pks[1])
      if len(partitionColumns) > 0 and len(prunedPartitionColumns) == 0:
        outerCondStr = f'{partitionColumnsExpr} AND {outerCondStr}'
      outerCondInitial = F.expr(outerCondStr)
      insertFilter = ' AND '.join([f'u.{pk} is null' for pk in pks[0]])
      updateFilter = ' AND '.join([f'u.{pk} is not null' for pk in pks[0]])
      outerWindowSpec = Window.partitionBy([f'__operation_flag'] + [f'u.{pk}' for pk in primaryKeys]).orderBy([F.desc(f'u.{pk}') for pk in primaryKeys] + [F.desc(f'staged_updates.{sc}') for sc in (sequenceColumns if sequenceColumns is not None else [])] + [F.expr('(' + ' + '.join([f'CASE WHEN staged_updates.{pk} is not null THEN 0 ELSE 1 END' for pk in pks[1]]) + ')')])
      condInitial = '__rn = 1 AND ' + condInitial
      updateCols = {c: F.col(f'staged_updates.__u_{c}') for c in deltaTableColumns}
    else:
      updateCols = {c: F.col(f'staged_updates.{c}') for c in deltaTableColumns}
    windowSpec = None
    if sequenceColumns is not None and len(sequenceColumns) > 0:
      windowSpec = Window.partitionBy(primaryKeys).orderBy([F.desc(sc) for sc in sequenceColumns] + [F.expr('(' + ' + '.join([f'CASE WHEN {c} is not null THEN 0 ELSE 1 END' for c in deltaTableColumns]) + ')')])
      matchCondition = ' AND '.join([f'(u.{sc} is null OR u.{sc} <= staged_updates.{"__u_" if len(pks[1]) > 0 else ""}{sc})' for sc in sequenceColumns])
    else:
      windowSpec = Window.partitionBy(primaryKeys).orderBy([F.expr('(' + ' + '.join([f'CASE WHEN {c} is not null THEN 0 ELSE 1 END' for c in deltaTableColumns]) + ')')])
    if outerCondInitial is not None:
      targetMergeKeyColumns = self._safeMergeLists(primaryKeys, [pc.column() for pc in partitionColumns])
      batchSelect = [F.col(f'staged_updates.{c}').alias(f'__u_{c}') for c in deltaTableColumns] + [F.expr(f'CASE WHEN __operation_flag = 2 THEN staged_updates.{c} WHEN __operation_flag = 1 THEN u.{c} END AS {c}') for c in targetMergeKeyColumns] + [F.when(F.expr('__operation_flag = 1'), F.row_number().over(outerWindowSpec)).otherwise(F.lit(2)).alias('__rn')]
      operationFlag = F.expr(f'CASE WHEN {updateFilter} THEN 1 WHEN {insertFilter} THEN 2 END').alias('__operation_flag')
      nullsCol = F.expr(' + '.join([f'CASE WHEN {pk} is not null THEN 0 ELSE 1 END' for pk in pks[1]]))
      stagedNullsCol = F.expr(' + '.join([f'CASE WHEN __u_{pk} is not null THEN 0 ELSE 1 END' for pk in pks[1]]))
      antiJoinCond = F.expr(' AND '.join([f'({outerCondStr})', '((u.__rn != 1 AND (u.__pk_nulls_count > staged_updates.__pk_nulls_count OR u.__u_pk_nulls_count > staged_updates.__u_pk_nulls_count)))', ' AND '.join([f'(u.__u_{pk} <=> staged_updates.__u_{pk} OR u.__u_{pk} is null)' for pk in pks[1]])]))
    def mergeFunc(batchDf, batchId):
      batchDf._jdf.sparkSession().conf().set('spark.databricks.optimizer.adaptive.enabled', True)
      batchDf._jdf.sparkSession().conf().set('spark.sql.adaptive.forceApply', True)
      deltaTable = deltaTableForFunc()
      mergeDf = None
      batchDf = self._dedupBatch(batchDf, windowSpec, primaryKeys)
      cond = condInitial
      if len(prunedPartitionColumns) > 0:
        partitionFilter = partitionColumnsExprFunc(batchDf)
        if partitionFilter is not None and len(partitionFilter) > 0:
          cond = f'({partitionFilter}) AND ({cond})'
      outerCond = outerCondInitial
      if outerCond is not None:
        if len(prunedPartitionColumns) > 0:
          if partitionFilter is not None and len(partitionFilter) > 0:
            outerCond = F.expr(partitionFilter) & outerCond
#         if 'product_id' in deltaTableColumns:
#           batchDf.withColumnRenamed('_commit_version', '__commit_version').write.format('delta').mode('overwrite').save('/Users/leon.eller@databricks.com/tmp/error/batch0')
        targetDf = deltaTable.toDF()
        u = targetDf.alias('u')
        su = F.broadcast(batchDf).alias('staged_updates')
        mergeDf = u.join(su, outerCond, 'right').select(F.col('*'), operationFlag).select(batchSelect).drop('__operation_flag').select(F.col('*'),
                                                                                                                                       nullsCol.alias('__pk_nulls_count'),
                                                                                                                                       stagedNullsCol.alias('__u_pk_nulls_count'))
        mergeDf = mergeDf.persist(StorageLevel.MEMORY_AND_DISK)
#         if 'product_id' in deltaTableColumns:
#           mergeDf.withColumnRenamed('_commit_version', '__commit_version').write.format('delta').mode('overwrite').save('/Users/leon.eller@databricks.com/tmp/error/merge')
        batchDf = mergeDf.alias('u').join(mergeDf.alias('staged_updates'), antiJoinCond, 'left_anti')
#         if 'product_id' in deltaTableColumns:
#           batchDf.withColumnRenamed('_commit_version', '__commit_version').write.format('delta').mode('overwrite').save('/Users/leon.eller@databricks.com/tmp/error/batch1')
      self._doMerge(deltaTable, cond, primaryKeys, windowSpec, updateCols, matchCondition, batchDf, batchId)
      if mergeDf is not None:
         mergeDf.unpersist()

    return StreamingJoin(self._left,
               self._right,
               self._joinType,
               mergeFunc).join(self._joinExpr,
                               self._transformFunc,
                               self._selectCols,
                               self._finalSelectCols)._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond)

  def stagingIndex(self):
    if self._dependentQuery is not None:
      return self._dependentQuery._depth(1)
    return 0

  def generateJoinName(self):
    name = f'$$_{self._left.name()}_{self._right.name()}_{self.stagingIndex()}'
    m = hashlib.sha256()
    m.update(self._left.path().encode('ascii'))
    m.update(self._right.path().encode('ascii'))
    return f'{name}/{self._joinType}/{m.hexdigest()}'

  def generateJoinStagingPath(self):
    dir = os.path.dirname(self._right.path())
    return f'{dir}/{self.generateJoinName()}'

  def _nonNullAndNullPrimaryKeys(self, joinType, leftPrimaryKeys, rightPrimaryKeys):
      if joinType == 'left':
        return [leftPrimaryKeys, rightPrimaryKeys]
      elif joinType == 'right':
        return [rightPrimaryKeys, leftPrimaryKeys]
      else:
        return [(leftPrimaryKeys + rightPrimaryKeys), []]

  def _createStagingStream(self, stagingPath, operationFunc):
    if stagingPath is None:
      stagingPath = self.generateJoinStagingPath()
    joinQuery = (
                  self.writeToPath(f'{stagingPath}/data')
                      .option('checkpointLocation', f'{stagingPath}/cp')
                      .queryName(self.generateJoinName())
                )
    primaryKeys = self._safeMergeLists(self._left.getPrimaryKeys(), self._right.getPrimaryKeys())
    if self._upstreamJoinCond is not None:
      def func():
        pks = self._upstreamJoinCond()
        pks1 = self._nonNullAndNullPrimaryKeys(self._joinType,
                                               [pk for pk in primaryKeys if pk in self._left.getPrimaryKeys()],
                                               [pk for pk in primaryKeys if pk in self._right.getPrimaryKeys()])
        return [self._mergeNonNullKeysForJoin(self._joinType, pks[0], pks[1], pks1[0], pks1[1]), self._mergeNullKeysForJoin(self._joinType, pks[0], pks[1], pks1[0], pks1[1])]
      joinCondFunc = func
    else:
      joinCondFunc = lambda: self._nonNullAndNullPrimaryKeys(self._joinType, [pk for pk in primaryKeys if pk in self._left.getPrimaryKeys()], [pk for pk in primaryKeys if pk in self._right.getPrimaryKeys()])
    return operationFunc(Stream.fromPath(f'{stagingPath}/data').setName(f'{self._left.name()}_{self._right.name()}').primaryKeys(*primaryKeys), joinQuery, joinCondFunc)

  def join(self, right, joinType = 'inner', stagingPath = None):
    return self._createStagingStream(stagingPath,
                          lambda stream, joinQuery, joinCondFunc: stream.join(right, joinType)._chainStreamingQuery(joinQuery, joinCondFunc))
  
  def groupBy(self, *cols, stagingPath = None):
    return self._createStagingStream(stagingPath,
                          lambda stream, joinQuery, joinCondFunc: stream.groupBy(*cols)._chainStreamingQuery(joinQuery, joinCondFunc))

  def writeToPath(self, path):
    return self._writeToTarget(lambda: DeltaTable.forPath(spark, path), f'delta.`{path}`', path)

  def writeToTable(self, tableName):
    return self._writeToTarget(lambda: DeltaTable.forName(spark, tableName), tableName, None)
    
class StreamToStreamJoinWithCondition:
  _left = None
  _right = None
  _joinType = None
  _joinExpr = None
  _transformFunc = None
  _dependentQuery = None
  _partitionColumns = None
  _upstreamJoinCond = None

  def __init__(self,
               left,
               right,
               joinType,
               onCondition,
               transformFunc = None,
               partitionColumns = None):
    self._left = left
    self._right = right
    self._joinType = joinType
    self._joinExpr = onCondition
    self._transformFunc = transformFunc
    self._partitionColumns = partitionColumns

  def _chainStreamingQuery(self, dependentQuery, upstreamJoinCond):
    self._dependentQuery = dependentQuery
    self._upstreamJoinCond = upstreamJoinCond
    return self

  def _to(self, func):
    if self._transformFunc is not None:
      tFunc = self._transformFunc
      newFunc = lambda f, l, r: func(tFunc(f, l, r), l, r)
    else:
      newFunc = func
    return StreamToStreamJoinWithCondition(self._left,
               self._right,
               self._joinType,
               self._joinExpr,
               newFunc,
               self._partitionColumns)._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond)
  
  def _selectColumns(self, leftCols, rightCols):
    leftStatic = self._left.static().select([c.columnName() for c in leftCols])
    rightStatic = self._right.static().select([c.columnName() for c in rightCols])
    schemaDf = leftStatic.join(rightStatic, self._joinExpr(leftStatic, rightStatic))
    if self._transformFunc is not None:
      schemaDf = self._transformFunc(schemaDf, leftStatic, rightStatic)
    def getColumns(joinedDf, oneSideDf):
      halfDf = joinedDf
      for k in oneSideDf.columns:
        halfDf = halfDf.drop(oneSideDf[k])
      return halfDf
    return [ColumnSelector(self._left, c) for c in getColumns(schemaDf, rightStatic).columns] + [ColumnSelector(self._right, c) for c in getColumns(schemaDf, leftStatic).columns]

  def stagingPath(self):
    return self.select('*').generateJoinStagingPath()

  def partitionBy(self, *columns):
    return self.select('*').partitionBy(*columns)

  def drop(self, column):
    if column.stream() == self._right.stream():
      func = lambda f, l, r: f.drop(r[column.columnName()])
    else:
      func = lambda f, l, r: f.drop(l[column.columnName()])
    return self.to(func)

  def to(self, func):
    return self._to(func)

  def join(self, right, joinType = 'inner', stagingPath = None):
    return self.select('*').join(right, joinType, stagingPath)

  def groupBy(self, *cols, stagingPath = None):
    return self.select("*").groupBy(*cols, stagingPath = stagingPath)

  def foreachBatch(self, mergeFunc):
    return self.select('*').foreachBatch(mergeFunc)

  def writeToPath(self, path):
    return self.select('*').writeToPath(path)

  def writeToTable(self, tableName):
    return self.select('*').writeToTable(tableName)
    
  def select(self, *selectCols):
    if isinstance(selectCols[0], ColumnSelector):
      leftDict = {}
      expandedCols = []
      for c in selectCols:
        if c.columnName() == '*':
          if c.stream() is self._left.stream():
            for col in self._left.columns():
              expandedCols.append(ColumnSelector(self._left, col))
          elif c.stream() is self._right.stream():
            for col in self._right.columns():
              expandedCols.append(ColumnSelector(self._right, col))
        else:
          expandedCols.append(c)
      selectCols = tuple(expandedCols)
      for c in selectCols:
        if c.stream() is self._left.stream():
          leftDict[c.columnName()] = c.columnName()
      def selectCol(c):
        cn = c.columnName()
        lc = leftDict.get(cn)
        if lc is not None:
          return lambda l, r: l[cn]
        return lambda l, r: r[cn]
      def finalSelectCol(c):
        cn = c.columnName()
        lc = leftDict.get(cn)
        if lc is not None:
          return lambda l, r: c.transform(l[cn])
        return lambda l, r: c.transform(r[cn])
      selectFuncs = [selectCol(c) for c in selectCols]
      selectFunc = lambda l, r: [f(l, r) for f in selectFuncs]
      finalSelectFuncs = [finalSelectCol(c) for c in selectCols]
      finalSelectFunc = lambda l, r: [f(l, r) for f in finalSelectFuncs]
    else:
      if isinstance(selectCols, tuple):
        # if '*' is specified convert to columns from left and right minus primary keys on right to avoid dups
        leftStars = [[ColumnSelector(self._left, lc) for lc in self._left.columns()] for c in selectCols if c == '*']
        rightStars = [[ColumnSelector(self._right, lc) for lc in self._right.columns()] for c in selectCols if c == '*']
        leftCols = [lc for arr in leftStars for lc in arr]
        rightCols = [lc for arr in rightStars for lc in arr]
        allCols = self._selectColumns(leftCols, rightCols)
        if len(allCols) > 0:
          return self.select(*allCols)
        else:
          return self.select(*([ColumnSelector(self._left, lc) for lc in self._left.columns() if lc in selectCols] + [ColumnSelector(self._right, lc) for lc in self._right.columns() if lc in selectCols]))
      else:
        selectFunc = selectCols
        finalSelectFunc = selectFunc
    return StreamToStreamJoinWithConditionForEachBatch(self._left,
               self._right,
               self._joinType,
               self._joinExpr,
               self._transformFunc,
               self._partitionColumns,
               selectFunc,
               finalSelectFunc)._chainStreamingQuery(self._dependentQuery, self._upstreamJoinCond)
