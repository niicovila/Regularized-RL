#!/bin/bash

# List of YAML files
yaml_files=(

    "/content/tests/lightning_research_framework/configs/configs_beta/test_5.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_6.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_7.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_10.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_11.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_12.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_13.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_14.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_16.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_17.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_18.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_19.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_20.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_21.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_22.yaml"
    "/content/tests/lightning_research_framework/configs/configs_beta/test_23.yaml"

)

# Loop through the YAML files and execute the command
counter=1
for yaml_file in "${yaml_files[@]}"
do
    output_path=".out_test_${counter}/"
    python scripts/train.py --config "$yaml_file" --path "$output_path"
    ((counter++))
done
