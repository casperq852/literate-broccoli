"""Azure Data Lake medallion refresh package.

This topic hosts the incremental *landing-zone -> bronze -> silver* pipeline that
replaces manual report uploads. Modules follow the a.s.r. MVC split: ``helpers``
holds pure path/name utilities, ``silver_model`` performs the report<->parquet
serialisation, and the ``*_controller`` modules own the Data Lake I/O and the
refresh orchestration.
"""
