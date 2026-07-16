pyarrow.lib.ArrowInvalid: This app has encountered an error. The original error message is redacted to prevent data leaks. Full error details have been recorded in the logs (if you're on Streamlit Cloud, click on 'Manage app' in the lower right of your app).
Traceback:
File "/mount/src/rinkos_pulsas/app.py", line 660, in <module>
    show_metines_page()
    ~~~~~~~~~~~~~~~~~^^
File "/mount/src/rinkos_pulsas/metines.py", line 2918, in show_metines_page
    _show_diagnostics(start_date, end_date)
    ~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^
File "/mount/src/rinkos_pulsas/metines.py", line 2668, in _show_diagnostics
    st.dataframe(metrics_df.head(200), use_container_width=True, hide_index=True)
    ~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
File "/home/adminuser/venv/lib/python3.14/site-packages/streamlit/runtime/metrics_util.py", line 568, in wrapped_func
    result = non_optional_func(*args, **kwargs)
File "/home/adminuser/venv/lib/python3.14/site-packages/streamlit/elements/arrow.py", line 977, in dataframe
    proto.arrow_data.data = dataframe_util.convert_pandas_df_to_arrow_bytes(
                            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        data_df
        ^^^^^^^
    )
    ^
File "/home/adminuser/venv/lib/python3.14/site-packages/streamlit/dataframe_util.py", line 970, in convert_pandas_df_to_arrow_bytes
    table = pa.Table.from_pandas(df)
File "pyarrow/table.pxi", line 4768, in pyarrow.lib.Table.from_pandas
File "/home/adminuser/venv/lib/python3.14/site-packages/pyarrow/pandas_compat.py", line 651, in dataframe_to_arrays
    arrays = [convert_column(c, f)
              ~~~~~~~~~~~~~~^^^^^^
File "/home/adminuser/venv/lib/python3.14/site-packages/pyarrow/pandas_compat.py", line 639, in convert_column
    raise e
File "/home/adminuser/venv/lib/python3.14/site-packages/pyarrow/pandas_compat.py", line 633, in convert_column
    result = pa.array(col, type=type_, from_pandas=True, safe=safe)
File "pyarrow/array.pxi", line 390, in pyarrow.lib.array
    result = _ndarray_to_array(values, mask, type, c_from_pandas, safe,
File "pyarrow/array.pxi", line 91, in pyarrow.lib._ndarray_to_array
    check_status(NdarrayToArrow(pool, values, mask, from_pandas,
File "pyarrow/error.pxi", line 92, in pyarrow.lib.check_status
