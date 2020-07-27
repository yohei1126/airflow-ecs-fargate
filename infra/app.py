#!/usr/bin/env python3

from aws_cdk import core

from stacks.airflow_stack import AirflowStack

app = core.App()
AirflowStack(app, "airflow")

app.synth()
