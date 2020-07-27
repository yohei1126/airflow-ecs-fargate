# airflow-ecs-fargate

# Environment Vriables

```
export AWS_REGION=ap-southeast-1
export AWS_ACCOUNT_ID=700136839063
export AWS_PROFILE=airflow-user
export TAG=1.10.9
```

# Authentication

```
aws ecr get-login-password --region $AWS_REGION --profile $AWS_PROFILE | \
    docker login --username AWS --password-stdin \
        $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
```

# Create a repo

# Push an image

```
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/airflow:$TAG
```

# Tutorial: Creating a Cluster with a Fargate Task Using the Amazon ECS CLI

* https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-cli-tutorial-fargate.html
