# tap-bing-ads

## Metadata Reference

tap-bing-ads uses some custom metadata keys:

* `fieldExclusions` - Indicates which other fields may not be selected when this field is selected. If you invoke the tap with selections that violate fieldExclusion rules, it is likely that the tap will fail.

---

Copyright &copy; 2017 Stitch

## Purpose of this branch

- The purpose of this branch is to fix an issue when tap-bing-ads generates its own schema (not following tap-catalog) and sometimes changes its schema.
- tap-bing-ads outputs schema which doesn't follow tap catalog / schema file.
- This can cause a pipeline failure if the new schema & data conflicts with existing schema & data in your BigQuery table.
- Our workaround is to hardcode schema in *type_map_user_defined_input.json* file to keep it fixed
- tap-bing-ads will output schema which will follow *type_map_user_defined_input.json* file.
- tap-bing-ads may output data which may **not** match *type_map_user_defined_input.json*.
- Our branch of target-bigquery will fix discrepancy between data and schema produced by tap-bing-ads, if it occurs.
- It will force data types in data to match data types in schema which is being passed to target-bigquery.
- Therefore, the combination of our branch of tap-bing-ads and target-bigquery should ensure consistent data types in schema and data 

## Usage

This branch of tap-bing-ads will give us control over schema which tap-bing-ads outputs.
``` 
### Install tap in its own virtualenv
RUN python3 -m pip install --upgrade pip
RUN python3 -m venv /pyenv/tap
RUN . /pyenv/tap/bin/activate && \
    pip install git+git://github.com/adswerve/tap-bing-ads@fix/supply-your-own-schema
``` 
This branch of target-bigquery will fix a potential discrepancy between data and schema which are being passed into target-bigquery.
``` 
###  Install target in its own virtualenv
RUN python3 -m pip install --upgrade pip
RUN python3 -m venv /pyenv/target
RUN . /pyenv/target/bin/activate \
    && pip install git+git://github.com/adswerve/target-bigquery@feature/updates-to-handling-schema

###  load data 
/pyenv/tap/bin/tap-bing-ads -config dt-tap-config.json -s dt-state.json --catalog dt-tap-catalog.json \ 
    | /pyenv/target/bin/target-bigquery -config dt-target-config.json 
```