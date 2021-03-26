# tap-bing-ads

## Metadata Reference

tap-bing-ads uses some custom metadata keys:

* `fieldExclusions` - Indicates which other fields may not be selected when this field is selected. If you invoke the tap with selections that violate fieldExclusion rules, it is likely that the tap will fail.

---

Copyright &copy; 2017 Stitch

## Purpose

- The purpose of this branch is to fix an issue when Bing Ads generates its own schema (not follow tap-catalog) and sometimes changes its schema.
- This can cause a pipeline failure if the new schema conflicts with existing schema in your BigQuery table.
- Tap Bing Ads outputs schema which doesn't follow tap catalog / schema file.
- Our workaround is to hardcode schema in type_map_user_defined_input.json file to keep it fixed
- Tap Bing Ads will output schema which will follow type_map_user_defined_input.json file
- Tap Bing Ads may output data which will not match type_map_user_defined_input.json
- Our branch of Target-BigQuery will fix discrepancy between data and schema produced by tap Bing Ads, if it occurs

## Usage

This branch of tap-bing-ads will give us control over schema which Tap Bing Ads outputs
``` 
### Install tap in its own virtualenv

RUN python3 -m pip install --upgrade pip
RUN python3 -m venv /pyenv/tap
RUN . /pyenv/tap/bin/activate && pip install git+git://github.com/adswerve/tap-bing-ads@fix/supply-your-own-schema
``` 
This branch of target-bigquery will fix a potential discrepancy between data and schema which are being passed into target-bigquery
``` 
###  Install target in its own virtualenv
RUN python3 -m pip install --upgrade pip
RUN python3 -m venv /pyenv/target
RUN . /pyenv/target/bin/activate && pip install git+git://github.com/adswerve/target-bigquery@feature/updates-to-handling-schema

###  load data 
/pyenv/tap/bin/tap-bing-ads -config dt-tap-config.json -s dt-state.json --catalog dt-tap-catalog.json | /pyenv/target/bin/target-bigquery -config dt-target-config.json 
```