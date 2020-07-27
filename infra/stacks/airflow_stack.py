from aws_cdk import (
    core,
    aws_rds as rds,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_ecr as ecr,
    aws_logs as logs,
    aws_elasticloadbalancingv2 as elb,
    aws_elasticloadbalancingv2_targets as elb_targets,
    aws_servicediscovery as sd,
    aws_elasticache as ec,
)
import os

ALB_PORT = 80
WEB_SERVER_PORT = 8080
POSTGRES_PORT = 5432
REDIS_PORT = 6379
FLOWER_PORT = 5555
MY_IP_CIDR = os.getenv("MY_IP_CIDR")


class AirflowStack(core.Stack):
    def __init__(self, app: core.App, id: str, **kwargs) -> None:
        super().__init__(app, id, **kwargs)

        # -- VPC
        vpc = ec2.Vpc(self, "vpc_airflow")
        # ecr
        ecr_repo = ecr.Repository.from_repository_name(
            self, "ecr_repo_airflow", "airflow"
        )
        # rds
        sg_airflow_backend_db = ec2.SecurityGroup(
            self,
            "sg_airflow_backend_database",
            vpc=vpc,
            description="Airflow backend database",
            security_group_name="sg_airflow_backend_database",
        )
        db = rds.DatabaseInstance(
            self,
            "rds_airfow_backend",
            master_username="postgres",
            master_user_password=core.SecretValue.plain_text("postgres"),
            database_name="airflow",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_11_8
            ),
            vpc=vpc,
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MICRO,
            ),
            instance_identifier="airflow-backend",
            removal_policy=core.RemovalPolicy.DESTROY,
            deletion_protection=False,
            security_groups=[sg_airflow_backend_db],
            vpc_placement=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )
        # -- ElasticCache Redis
        sg_redis = ec2.SecurityGroup(
            self,
            "sg_redis",
            vpc=vpc,
            description="Airflow redis",
            security_group_name="sg_redis",
        )
        redis_subnet_group = ec.CfnSubnetGroup(
            self,
            "airflow-redis-subnet-group",
            description="For Airflow Task Queue",
            subnet_ids=vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE
            ).subnet_ids,
            cache_subnet_group_name="airflow-redis-task-queue",
        )
        redis = ec.CfnCacheCluster(
            self,
            "redis",
            cluster_name="airflow-redis",
            cache_node_type="cache.t2.micro",
            engine="redis",
            num_cache_nodes=1,
            auto_minor_version_upgrade=True,
            engine_version="5.0.6",
            port=REDIS_PORT,
            cache_subnet_group_name=redis_subnet_group.ref,
            vpc_security_group_ids=[sg_redis.security_group_id],
        )
        # ECS cluster
        cluster = ecs.Cluster(
            self,
            "ecs_airflow",
            cluster_name="airflow",
            vpc=vpc,
            container_insights=True,
        )
        # scheduler
        scheduler_task_role = iam.Role(
            self,
            "iam_role_scheduler",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="IAM role for ECS Scheduler service",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonEC2ContainerRegistryReadOnly"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchLogsFullAccess"
                ),
            ],
            role_name="airflow-ecs-scheduler-task",
        )
        scheduler_task = ecs.FargateTaskDefinition(
            self,
            "ecs_task_scheduler",
            cpu=512,
            memory_limit_mib=2048,
            task_role=scheduler_task_role,
        )
        scheduler_task.add_container(
            "scheduler",
            command=["scheduler"],
            # credentials should be provided from Secrets Manager
            environment={
                "LOAD_EX": "n",
                "FERNET_KEY": "46BKJoQYlPPOexq0OhDZnIlNepKFf87WFwLbfzqDDho=",
                "EXECUTOR": "Celery",
                "POSTGRES_HOST": db.db_instance_endpoint_address,
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "postgres",
                "POSTGRES_DB": "airflow",
                "REDIS_HOST": redis.attr_redis_endpoint_address,
            },
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, "1.10.9",),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="scheduler",
                log_group=logs.LogGroup(
                    self,
                    "log-airflow-scheduler",
                    log_group_name="ecs/airflow/scheduler",
                    retention=logs.RetentionDays.ONE_WEEK,
                ),
            ),
        )
        sg_airflow_scheduler = ec2.SecurityGroup(
            self,
            "sg_airflow_scheduler",
            vpc=vpc,
            description="Airflow Scheduler service",
            security_group_name="sg_airflow_scheduler",
        )
        sg_redis.add_ingress_rule(
            peer=sg_airflow_scheduler,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="from scheduler",
                from_port=REDIS_PORT,
                to_port=REDIS_PORT,
            ),
            description="from scheduler service",
        )
        sg_airflow_backend_db.add_ingress_rule(
            peer=sg_airflow_scheduler,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="from home",
                from_port=POSTGRES_PORT,
                to_port=POSTGRES_PORT,
            ),
            description="home",
        )
        scheduler_service = ecs.FargateService(
            self,
            "ecs_service_scheduler",
            cluster=cluster,
            task_definition=scheduler_task,
            desired_count=1,
            security_groups=[sg_airflow_scheduler],
            service_name="scheduler",
        )
        # flower
        flower_task_role = iam.Role(
            self,
            "iam_role_flower",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="IAM role for ECS Flower service",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonEC2ContainerRegistryReadOnly"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchLogsFullAccess"
                ),
            ],
            role_name="airflow-ecs-flower-task",
        )
        flower_task = ecs.FargateTaskDefinition(
            self,
            "ecs_task_flower",
            cpu=512,
            memory_limit_mib=1024,
            task_role=scheduler_task_role,
        )
        flower_task.add_container(
            "flower",
            command=["flower"],
            # credentials should be provided from Secrets Manager
            environment={
                "LOAD_EX": "n",
                "FERNET_KEY": "46BKJoQYlPPOexq0OhDZnIlNepKFf87WFwLbfzqDDho=",
                "EXECUTOR": "Celery",
                "REDIS_HOST": redis.attr_redis_endpoint_address,
            },
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, "1.10.9",),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="flower",
                log_group=logs.LogGroup(
                    self,
                    "log-airflow-flower",
                    log_group_name="ecs/airflow/flower",
                    retention=logs.RetentionDays.ONE_WEEK,
                ),
            ),
        ).add_port_mappings(
            ecs.PortMapping(
                container_port=FLOWER_PORT,
                host_port=FLOWER_PORT,
                protocol=ecs.Protocol.TCP,
            )
        )
        sg_airflow_flower = ec2.SecurityGroup(
            self,
            "sg_airflow_flower",
            vpc=vpc,
            description="Airflow Flower service",
            security_group_name="sg_airflow_flower",
        )
        sg_airflow_flower.add_ingress_rule(
            peer=ec2.Peer.ipv4("115.66.217.45/32"),
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="from homr",
                from_port=FLOWER_PORT,
                to_port=FLOWER_PORT,
            ),
            description="from home",
        )
        sg_redis.add_ingress_rule(
            peer=sg_airflow_flower,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="from flower",
                from_port=REDIS_PORT,
                to_port=REDIS_PORT,
            ),
            description="from flower",
        )
        flower_service = ecs.FargateService(
            self,
            "ecs_service_flower",
            cluster=cluster,
            task_definition=flower_task,
            desired_count=1,
            security_groups=[sg_airflow_flower],
            service_name="flower",
        )
        # worker
        worker_task_role = iam.Role(
            self,
            "iam_role_worker",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="IAM role for ECS worker service",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonEC2ContainerRegistryReadOnly"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchLogsFullAccess"
                ),
            ],
            role_name="airflow-ecs-worker-task",
        )
        worker_task = ecs.FargateTaskDefinition(
            self,
            "ecs_task_worker",
            cpu=1024,
            memory_limit_mib=3072,
            task_role=worker_task_role,
        )
        worker_task.add_container(
            "worker",
            command=["worker"],
            # credentials should be provided from Secrets Manager
            environment={
                "LOAD_EX": "n",
                "FERNET_KEY": "46BKJoQYlPPOexq0OhDZnIlNepKFf87WFwLbfzqDDho=",
                "EXECUTOR": "Celery",
                "POSTGRES_HOST": db.db_instance_endpoint_address,
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "postgres",
                "POSTGRES_DB": "airflow",
                "REDIS_HOST": redis.attr_redis_endpoint_address,
            },
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, "1.10.9",),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="worker",
                log_group=logs.LogGroup(
                    self,
                    "log-airflow-worker",
                    log_group_name="ecs/airflow/worker",
                    retention=logs.RetentionDays.ONE_WEEK,
                ),
            ),
        )
        sg_airflow_worker = ec2.SecurityGroup(
            self,
            "sg_airflow_worker",
            vpc=vpc,
            description="Airflow worker service",
            security_group_name="sg_airflow_worker",
        )
        sg_redis.add_ingress_rule(
            peer=sg_airflow_worker,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="from worker",
                from_port=REDIS_PORT,
                to_port=REDIS_PORT,
            ),
            description="from worker service",
        )
        sg_airflow_backend_db.add_ingress_rule(
            peer=sg_airflow_worker,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="from worker",
                from_port=POSTGRES_PORT,
                to_port=POSTGRES_PORT,
            ),
            description="From worker",
        )
        worker_service = ecs.FargateService(
            self,
            "ecs_service_worker",
            cluster=cluster,
            task_definition=worker_task,
            desired_count=1,
            security_groups=[sg_airflow_worker],
            service_name="worker",
        )
        # web server
        web_server_task_role = iam.Role(
            self,
            "iam_role_web_server",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="IAM role for ECS web server service",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonEC2ContainerRegistryReadOnly"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchLogsFullAccess"
                ),
            ],
            role_name="airflow-ecs-web-server-task",
        )
        web_server_task = ecs.FargateTaskDefinition(
            self,
            "ecs_task_web_server",
            cpu=512,
            memory_limit_mib=1024,
            task_role=web_server_task_role,
        )
        web_server_task.add_container(
            "web_server",
            command=["webserver"],
            # credentials should be provided from Secrets Manager
            environment={
                "LOAD_EX": "n",
                "FERNET_KEY": "46BKJoQYlPPOexq0OhDZnIlNepKFf87WFwLbfzqDDho=",
                "EXECUTOR": "Celery",
                "POSTGRES_HOST": db.db_instance_endpoint_address,
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "postgres",
                "POSTGRES_DB": "airflow",
                "REDIS_HOST": redis.attr_redis_endpoint_address,
            },
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, "1.10.9",),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="web_server",
                log_group=logs.LogGroup(
                    self,
                    "log-airflow-web-server",
                    log_group_name="ecs/airflow/web-server",
                    retention=logs.RetentionDays.ONE_WEEK,
                ),
            ),
        ).add_port_mappings(
            ecs.PortMapping(
                container_port=WEB_SERVER_PORT,
                host_port=WEB_SERVER_PORT,
                protocol=ecs.Protocol.TCP,
            )
        )
        sg_airflow_web_server = ec2.SecurityGroup(
            self,
            "sg_airflow_web_server",
            vpc=vpc,
            description="Airflow web server service",
            security_group_name="sg_airflow_web_server",
        )
        sg_airflow_backend_db.add_ingress_rule(
            peer=sg_airflow_web_server,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="From web server",
                from_port=POSTGRES_PORT,
                to_port=POSTGRES_PORT,
            ),
            description="From web server",
        )
        sg_airflow_backend_db.add_ingress_rule(
            peer=sg_airflow_web_server,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="From web server",
                from_port=POSTGRES_PORT,
                to_port=POSTGRES_PORT,
            ),
            description="From web server",
        )
        sg_redis.add_ingress_rule(
            peer=sg_airflow_web_server,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="from web server",
                from_port=REDIS_PORT,
                to_port=REDIS_PORT,
            ),
            description="from web server",
        )
        web_server_service = ecs.FargateService(
            self,
            "ecs_service_web_server",
            cluster=cluster,
            task_definition=web_server_task,
            desired_count=1,
            security_groups=[sg_airflow_web_server],
            service_name="web_server",
        )
        # Load balancer
        sg_airflow_alb = ec2.SecurityGroup(
            self,
            "sg_airflow_alb",
            vpc=vpc,
            description="Airflow ALB",
            security_group_name="sg_airflow_alb",
        )
        # ALB -> web server
        sg_airflow_web_server.add_ingress_rule(
            peer=sg_airflow_alb,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="From ALB",
                from_port=WEB_SERVER_PORT,
                to_port=WEB_SERVER_PORT,
            ),
            description="From ALB",
        )
        # ALB -> flower
        sg_airflow_flower.add_ingress_rule(
            peer=sg_airflow_alb,
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="From ALB",
                from_port=FLOWER_PORT,
                to_port=FLOWER_PORT,
            ),
            description="From ALB",
        )
        # Home -> ALB
        sg_airflow_alb.add_ingress_rule(
            peer=ec2.Peer.ipv4(MY_IP_CIDR),
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="From Home",
                from_port=ALB_PORT,
                to_port=ALB_PORT,
            ),
            description="From Home",
        )
        # Home -> ALB
        sg_airflow_alb.add_ingress_rule(
            peer=ec2.Peer.ipv4(MY_IP_CIDR),
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="From Home",
                from_port=FLOWER_PORT,
                to_port=FLOWER_PORT,
            ),
            description="From Home",
        )
        alb = elb.ApplicationLoadBalancer(
            self,
            "alb_airflow",
            internet_facing=True,
            security_group=sg_airflow_alb,
            vpc=vpc,
            load_balancer_name="alb-airflow",
        )
        listener1 = alb.add_listener(
            "alb_airflow_listener1",
            open=False,
            port=ALB_PORT,
            protocol=elb.ApplicationProtocol.HTTP,
            default_target_groups=[
                elb.ApplicationTargetGroup(
                    self,
                    "alb_airflow_target_group_web_server",
                    port=WEB_SERVER_PORT,
                    protocol=elb.ApplicationProtocol.HTTP,
                    target_group_name="alb-tg-airflow-web-server",
                    targets=[web_server_service],
                    vpc=vpc,
                )
            ],
        )
        alb.add_listener(
            "alb_airflow_listener2",
            open=False,
            port=FLOWER_PORT,
            protocol=elb.ApplicationProtocol.HTTP,
            default_target_groups=[
                elb.ApplicationTargetGroup(
                    self,
                    "alb_airflow_target_group_flower",
                    port=FLOWER_PORT,
                    protocol=elb.ApplicationProtocol.HTTP,
                    target_group_name="alb-tg-aiflow-flower",
                    targets=[flower_service],
                    vpc=vpc,
                )
            ],
        )
