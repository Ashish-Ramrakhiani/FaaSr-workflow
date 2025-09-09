#!/usr/bin/env python3

import argparse
import json
import os
import sys
import boto3
from github import Github
import base64
import tempfile
import shutil
import subprocess
import requests
import time
import logging
from collections import defaultdict
import re

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def parse_arguments():
    parser = argparse.ArgumentParser(description='Deploy FaaSr functions to specified platform')
    parser.add_argument('--workflow-file', required=True,
                      help='Path to the workflow JSON file')
    return parser.parse_args()

def read_workflow_file(file_path):
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: Workflow file {file_path} not found")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in workflow file {file_path}")
        sys.exit(1)

def extract_rank(str_input):
    """
    Returns action name and rank of an action with rank (e.g func(7) returns (func, 7))

    Arguments:
        str_input: function name with rank
    Returns:
        (str, int) -- action name and rank
    """
    parts = str_input.split("(")
    if len(parts) != 2 or not parts[1].endswith(")"):
        return str_input, 1
    rank = int(parts[1][:-1])
    action_name = parts[0]
    return (action_name, rank)

def is_cyclic(adj_graph, curr, visited, stack):
    """
    Recursive function that if there is a cycle in a directed
    graph defined by an adjacency list

    Arguments:
        adj_graph: adjacency list for graph (dict)
        curr: current node
        visited: set of visited nodes (set)
        stack: list of nodes in recursion call stack (list)

    Returns:
        bool: True if cycle exists, False otherwise
    """
    # if the current node is in the recursion call
    # stack then there must be a cycle in the graph
    if curr in stack:
        return True

    # add current node to recursion call stack and visited set
    visited.add(curr)
    stack.append(curr)

    # check each successor for cycles, recursively calling is_cyclic()
    for child in adj_graph[curr]:
        if child not in visited and is_cyclic(adj_graph, child, visited, stack):
            logger.error(f"Function loop found from node {curr} to {child}")
            sys.exit(1)
        elif child in stack:
            logger.error(f"Function loop found from node {curr} to {child}")
            sys.exit(1)

    # no more successors to visit for this branch and no cycles found
    # remove current node from recursion call stack
    stack.pop()
    return False

def build_adjacency_graph(payload):
    """
    This function builds an adjacency list for the FaaSr workflow graph and determines
    the ranks of each action

    Arguments:
        payload: FaaSr payload dict
    Returns:
        adj_graph: dict of predecessor: successor pairs
        rank: dict of each action's rank
    """
    adj_graph = defaultdict(list)
    ranks = dict()

    # Build adjacency list from ActionList
    for func in payload["ActionList"].keys():
        invoke_next = payload["ActionList"][func]["InvokeNext"]
        if isinstance(invoke_next, str):
            invoke_next = [invoke_next]
        for child in invoke_next:

            def process_action(action):
                action_name, action_rank = extract_rank(action)
                if action_name in ranks and ranks[action_name] > 1:
                    err_msg = "Function with rank cannot have multiple predecessors"
                    logger.error(err_msg)
                    sys.exit(1)
                else:
                    adj_graph[func].append(action_name)
                    ranks[action_name] = action_rank

            if isinstance(child, dict):
                for conditional_branch in child.values():
                    for action in conditional_branch:
                        process_action(action)
            else:
                process_action(child)

    for func in adj_graph:
        if func not in ranks:
            ranks[func] = 0

    return (adj_graph, ranks)

def predecessors_list(adj_graph):
    """This function returns a map of action predecessor pairs

    Arguments:
        adj_graph: adjacency list for graph -- dict(function: successor)
    """
    pre = defaultdict(list)
    for func1 in adj_graph:
        for func2 in adj_graph[func1]:
            pre[func2].append(func1)
    return pre

def check_dag(faasr_payload):
    """
    This method checks for cycles, repeated function names,
    or unreachable nodes in the workflow and aborts if it finds any

    Arguments:
        payload: FaaSr payload dict
    Returns:
        predecessors: dict -- map of function predecessors
    """
    if faasr_payload["FunctionInvoke"] not in faasr_payload["ActionList"]:
        err_msg = "FunctionInvoke does not refer to a valid function"
        logger.error(err_msg)
        sys.exit(1)

    adj_graph, ranks = build_adjacency_graph(faasr_payload)

    # Initialize empty recursion call stack
    stack = []

    # Initialize empty visited set
    visited = set()

    # Find initial function in the graph
    start = False
    for func in faasr_payload["ActionList"]:
        if ranks[func] == 0:
            start = True
            # This function stores the first function with no predecessors
            # In the cases where there is multiple functions with no
            # predecessors, an unreachable state error will occur later
            first_func = func
            break

    # Ensure there is an initial action
    if start is False:
        logger.error("Function loop found: no initial action")
        sys.exit(1)

    # Check for cycles
    is_cyclic(adj_graph, first_func, visited, stack)

    # Check if all of the functions have been visited by the DFS
    # If not, then there is an unreachable state in the graph
    for func in faasr_payload["ActionList"]:
        if func.split(".")[0] not in visited:
            logger.error(f"Unreachable state found: {func}")
            sys.exit(1)

    # Initialize predecessor list
    pre = predecessors_list(adj_graph)

    curr_pre = pre[faasr_payload["FunctionInvoke"]]
    real_pre = []
    for p in curr_pre:
        if p in ranks and ranks[p] > 1:
            for i in range(1, ranks[p] + 1):
                real_pre.append(f"{p}.{i}")
        else:
            real_pre.append(p)
    return real_pre

def get_github_token():
    # Get GitHub PAT from environment variable
    token = os.getenv('GITHUB_TOKEN')
    if not token:
        print("Error: GITHUB_TOKEN environment variable not set")
        sys.exit(1)
    return token

def get_aws_credentials():
    # Try to get AWS credentials from environment variables
    aws_access_key = os.getenv('AWS_ACCESS_KEY_ID')
    aws_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')
    aws_region = 'us-east-1'
    role_arn = os.getenv('AWS_LAMBDA_ROLE_ARN')
    
    if not all([aws_access_key, aws_secret_key, role_arn]):
        print("Error: AWS credentials or role ARN not set in environment variables")
        sys.exit(1)
    
    return aws_access_key, aws_secret_key, aws_region, role_arn

def set_github_variable(repo_full_name, var_name, var_value, github_token):
    url = f"https://api.github.com/repos/{repo_full_name}/actions/variables/{var_name}"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json"
    }
    data = {"name": var_name, "value": var_value}
    # Try to update, if not found, create
    r = requests.patch(url, headers=headers, json=data)
    if r.status_code == 404:
        r = requests.post(f"https://api.github.com/repos/{repo_full_name}/actions/variables", headers=headers, json=data)
    if not r.ok:
        print(f"Failed to set variable {var_name}: {r.text}")
    else:
        print(f"Set variable {var_name} for {repo_full_name}")

def ensure_github_secrets_and_vars(repo, required_secrets, required_vars, github_token):
    """Set GitHub secrets and variables for the repository."""
    # Check and set secrets
    existing_secrets = {s.name for s in repo.get_secrets()}
    for secret_name, secret_value in required_secrets.items():
        if secret_name not in existing_secrets:
            print(f"Setting secret: {secret_name}")
        else:
            print(f"Secret {secret_name} already exists, updating it.")
        repo.create_secret(secret_name, secret_value)

    # Set variables using REST API
    for var_name, var_value in required_vars.items():
        set_github_variable(repo.full_name, var_name, var_value, github_token)

def create_secret_payload(workflow_data):
    """
    Create a secret payload that combines all necessary credentials and the complete workflow configuration.
    This payload will be stored as a GitHub secret and used by the deployed functions.
    This function matches the logic from build_faasr_payload in trigger_function.py
    """
    # Start with credentials at the top
    credentials = {
        "My_GitHub_Account_TOKEN": get_github_token(),
        "My_Minio_Bucket_ACCESS_KEY": os.getenv('MINIO_ACCESS_KEY'),
        "My_Minio_Bucket_SECRET_KEY": os.getenv('MINIO_SECRET_KEY'),
        "My_OW_Account_API_KEY": os.getenv('OW_API_KEY', ''),
        "My_Lambda_Account_ACCESS_KEY": os.getenv('AWS_ACCESS_KEY_ID', ''),
        "My_Lambda_Account_SECRET_KEY": os.getenv('AWS_SECRET_ACCESS_KEY', ''),
    }
    
    payload = credentials.copy()

    # Add workflow data (excluding _workflow_file)
    workflow_copy = workflow_data.copy()
    if '_workflow_file' in workflow_copy:
        del workflow_copy['_workflow_file']
    payload.update(workflow_copy)
    
    # Replace placeholder values in ComputeServers with actual credentials
    if 'ComputeServers' in payload:
        for server_key, server_config in payload['ComputeServers'].items():
            faas_type = server_config.get('FaaSType', '')
            
            # Replace placeholder values with actual credentials
            if faas_type == 'Lambda':
                # Replace Lambda AccessKey/SecretKey placeholders
                if 'AccessKey' in server_config and server_config['AccessKey'] == f"{server_key}_ACCESS_KEY":
                    if credentials['My_Lambda_Account_ACCESS_KEY']:
                        server_config['AccessKey'] = credentials['My_Lambda_Account_ACCESS_KEY']
                if 'SecretKey' in server_config and server_config['SecretKey'] == f"{server_key}_SECRET_KEY":
                    if credentials['My_Lambda_Account_SECRET_KEY']:
                        server_config['SecretKey'] = credentials['My_Lambda_Account_SECRET_KEY']
            elif faas_type == 'GitHubActions':
                # Replace GitHub Token placeholder
                if 'Token' in server_config and server_config['Token'] == f"{server_key}_TOKEN":
                    if credentials['My_GitHub_Account_TOKEN']:
                        server_config['Token'] = credentials['My_GitHub_Account_TOKEN']
            elif faas_type == 'OpenWhisk':
                # Replace OpenWhisk API.key placeholder
                if 'API.key' in server_config and server_config['API.key'] == f"{server_key}_API_KEY":
                    if credentials['My_OW_Account_API_KEY']:
                        server_config['API.key'] = credentials['My_OW_Account_API_KEY']

    # Replace placeholder values in DataStores with actual credentials
    if 'DataStores' in payload:
        for store_key, store_config in payload['DataStores'].items():
            # Replace placeholder values with actual credentials
            if 'AccessKey' in store_config and store_config['AccessKey'] == f"{store_key}_ACCESS_KEY":
                if store_key == 'My_Minio_Bucket' and credentials['My_Minio_Bucket_ACCESS_KEY']:
                    store_config['AccessKey'] = credentials['My_Minio_Bucket_ACCESS_KEY']
            if 'SecretKey' in store_config and store_config['SecretKey'] == f"{store_key}_SECRET_KEY":
                if store_key == 'My_Minio_Bucket' and credentials['My_Minio_Bucket_SECRET_KEY']:
                    store_config['SecretKey'] = credentials['My_Minio_Bucket_SECRET_KEY']
    
    return json.dumps(payload)

def deploy_to_github(workflow_data):
    """Deploy functions to GitHub Actions."""
    github_token = get_github_token()
    g = Github(github_token)
    
    # Get the workflow name for prefixing
    workflow_name = workflow_data.get('WorkflowName', 'default')
    json_prefix = workflow_name
    
    # Get the current repository
    repo_name = os.getenv('GITHUB_REPOSITORY')
    if not repo_name:
        print("Error: GITHUB_REPOSITORY environment variable not set")
        sys.exit(1)
    
    # Filter actions that should be deployed to GitHub Actions
    github_actions = {}
    for action_name, action_data in workflow_data['ActionList'].items():
        server_name = action_data['FaaSServer']
        server_config = workflow_data['ComputeServers'][server_name]
        faas_type = server_config['FaaSType'].lower()
        if faas_type in ['githubactions', 'github_actions', 'github']:
            github_actions[action_name] = action_data
    
    if not github_actions:
        print("No actions found for GitHub Actions deployment")
        return
    
    try:
        repo = g.get_repo(repo_name)
        
        # Get the default branch name
        default_branch = repo.default_branch
        print(f"Using branch: {default_branch}")
        
        # Create secret payload and set up secrets/variables
        secret_payload = create_secret_payload(workflow_data)
        required_secrets = {"SECRET_PAYLOAD": secret_payload}
        vars = {f"{json_prefix.upper()}_PAYLOAD_REPO": f"{repo_name}/{workflow_data['_workflow_file']}"}
        
        ensure_github_secrets_and_vars(repo, required_secrets, vars, github_token)
        
        # Deploy each action
        for action_name, action_data in github_actions.items():
            actual_func_name = action_data['FunctionName']
            
            # Create prefixed action name using workflow_name-action_name format
            prefixed_action_name = f"{json_prefix}-{action_name}"
            
            # Create workflow file
            # Get container image, with fallback to default
            container_image = workflow_data.get('ActionContainers', {}).get(action_name, 'ghcr.io/faasr/github-actions-tidyverse')
            
            workflow_content = f"""name: {prefixed_action_name}

on:
  workflow_dispatch:
    inputs:
      OVERWRITTEN:
        description: 'overwritten fields'
        required: true
      PAYLOAD_URL:
        description: 'url to payload'
        required: true
jobs:
  run_docker_image:
    runs-on: ubuntu-latest
    container: {container_image}
    env:
      TOKEN: ${{{{ secrets.My_GitHub_Account_PAT }}}}
      My_GitHub_Account_PAT: ${{{{ secrets.My_GitHub_Account_PAT }}}}
      My_S3_Bucket_AccessKey: ${{{{ secrets.My_S3_Bucket_AccessKey }}}}
      My_S3_Bucket_SecretKey: ${{{{ secrets.My_S3_Bucket_SecretKey }}}}
      OVERWRITTEN: ${{{{ github.event.inputs.OVERWRITTEN }}}}
      PAYLOAD_URL: ${{{{ github.event.inputs.PAYLOAD_URL }}}}
    steps:
    - name: run Python
      run: |
        cd /action
        python3 faasr_entry.py
"""

            # Create or update the workflow file
            workflow_path = f".github/workflows/{prefixed_action_name}.yml"
            try:
                # Try to get the file first
                contents = repo.get_contents(workflow_path)
                existing_content = contents.decoded_content.decode('utf-8')
                
                # Check if content has changed
                if existing_content.strip() == workflow_content.strip():
                    print(f"File {workflow_path} content is already up to date, skipping update")
                else:
                    # If file exists and content is different, update it
                    print(f"File {workflow_path} exists, updating...")
                    repo.update_file(
                        path=workflow_path,
                        message=f"Update workflow for {prefixed_action_name}",
                        content=workflow_content,
                        sha=contents.sha,
                        branch=default_branch
                    )
                    print(f"Successfully updated {workflow_path}")
            except Exception as e:
                if "Not Found" in str(e) or "404" in str(e):
                    # If file doesn't exist, create it
                    print(f"File {workflow_path} doesn't exist, creating...")
                    repo.create_file(
                        path=workflow_path,
                        message=f"Add workflow for {prefixed_action_name}",
                        content=workflow_content,
                        branch=default_branch
                    )
                    print(f"Successfully created {workflow_path}")
                else:
                    print(f"Error updating/creating {workflow_path}: {str(e)}")
                    # Try to get more details about the error
                    if hasattr(e, 'data'):
                        print(f"Error details: {e.data}")
                    if hasattr(e, 'status'):
                        print(f"HTTP status: {e.status}")
                    raise e
                    
            print(f"Successfully deployed {prefixed_action_name} to GitHub")
            
    except Exception as e:
        print(f"Error deploying to GitHub: {str(e)}")
        sys.exit(1)

def deploy_to_aws(workflow_data):
    # Get AWS credentials
    aws_access_key, aws_secret_key, aws_region, role_arn = get_aws_credentials()
    
    lambda_client = boto3.client(
        'lambda',
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region
    )
    
    # Get the workflow name for function naming
    workflow_name = workflow_data.get('WorkflowName', 'default')
    json_prefix = workflow_name
    
    # Create secret payload (same as GitHub deployment)
    secret_payload = create_secret_payload(workflow_data)
    
    # Filter actions that should be deployed to AWS Lambda
    lambda_actions = {}
    for action_name, action_data in workflow_data['ActionList'].items():
        server_name = action_data['FaaSServer']
        server_config = workflow_data['ComputeServers'][server_name]
        faas_type = server_config['FaaSType'].lower()
        if faas_type in ['lambda', 'aws_lambda', 'aws']:
            lambda_actions[action_name] = action_data
    
    if not lambda_actions:
        print("No actions found for AWS Lambda deployment")
        return
    
    # Process each action in the workflow
    for action_name, action_data in lambda_actions.items():
        try:
            actual_func_name = action_data['FunctionName']
            
            # Create prefixed function name using workflow_name-action_name format
            prefixed_func_name = f"{json_prefix}-{action_name}"
            
            # Get container image for AWS Lambda (must be an Amazon ECR image URI)
            container_image = workflow_data.get('ActionContainers', {}).get(action_name)
            if not container_image:
                container_image = '145342739029.dkr.ecr.us-east-1.amazonaws.com/aws-lambda-tidyverse:latest'
                print(f"No container specified for action '{action_name}', using default: {container_image}")
 
            # Check payload size before deployment
            payload_size = len(secret_payload.encode('utf-8'))
            if payload_size > 4000:  # Lambda env var limit is ~4KB
                print(f"Warning: SECRET_PAYLOAD size ({payload_size} bytes) may exceed Lambda environment variable limits")
                print("Consider using Parameter Store or S3 for large payloads")
            
            # Environment variables for Lambda function
            environment_vars = {
                'SECRET_PAYLOAD': secret_payload
            }
            
            # Check if function already exists first
            try:
                existing_func = lambda_client.get_function(FunctionName=prefixed_func_name)
                print(f"Function {prefixed_func_name} already exists, updating...")
                # Update existing function
                lambda_client.update_function_code(
                    FunctionName=prefixed_func_name,
                    ImageUri=container_image
                )
                
                # Wait for the function update to complete
                print(f"Waiting for {prefixed_func_name} code update to complete...")
                max_attempts = 60  # Wait up to 5 minutes
                attempt = 0
                while attempt < max_attempts:
                    try:
                        response = lambda_client.get_function(FunctionName=prefixed_func_name)
                        state = response['Configuration']['State']
                        last_update_status = response['Configuration']['LastUpdateStatus']
                        
                        if state == 'Active' and last_update_status == 'Successful':
                            break
                        elif state == 'Failed' or last_update_status == 'Failed':
                            sys.exit(1)
                        else:
                            time.sleep(5)
                            attempt += 1
                    except Exception as e:
                        print(f"Error checking function state: {str(e)}")
                        time.sleep(5)
                        attempt += 1
                
                if attempt >= max_attempts:
                    print(f"Timeout waiting for {prefixed_func_name} update to complete")
                    sys.exit(1)
                
                # Now update environment variables
                lambda_client.update_function_configuration(
                    FunctionName=prefixed_func_name,
                    Environment={'Variables': environment_vars}
                )
                print(f"Successfully updated {prefixed_func_name} on AWS Lambda")
                
            except lambda_client.exceptions.ResourceNotFoundException:
                # Function doesn't exist, create it
                print(f"Creating new Lambda function: {prefixed_func_name}")
                
                # Create function with minimal parameters first, then update
                print("Creating with minimal parameters...")
                try:
                    lambda_client.create_function(
                        FunctionName=prefixed_func_name,
                        PackageType='Image',
                        Code={'ImageUri': container_image},
                        Role=role_arn,
                        Timeout=300,  # Shorter timeout
                        MemorySize=128,  # Minimal memory
                    )
                    print(f"Successfully created {prefixed_func_name} with minimal parameters")
                    
                    # Wait for the function to become active before updating
                    print(f"Waiting for {prefixed_func_name} to become active...")
                    max_attempts = 60  # Wait up to 5 minutes
                    attempt = 0
                    while attempt < max_attempts:
                        try:
                            response = lambda_client.get_function(FunctionName=prefixed_func_name)
                            state = response['Configuration']['State']
                            
                            if state == 'Active':
                                print(f"Function {prefixed_func_name} is now active")
                                break
                            elif state == 'Failed':
                                print(f"Function {prefixed_func_name} creation failed")
                                sys.exit(1)
                            else:
                                print(f"Function state: {state}, waiting...")
                                time.sleep(5)
                                attempt += 1
                        except Exception as e:
                            print(f"Error checking function state: {str(e)}")
                            time.sleep(5)
                            attempt += 1
                    
                    if attempt >= max_attempts:
                        print(f"Timeout waiting for {prefixed_func_name} to become active")
                        sys.exit(1)
                    
                    # Now update with full configuration
                    lambda_client.update_function_configuration(
                        FunctionName=prefixed_func_name,
                        Timeout=900,
                        MemorySize=1024,
                        Environment={'Variables': environment_vars}
                    )
                    print(f"Updated {prefixed_func_name} with full configuration")
                    
                except Exception as minimal_error:
                    print(f"Minimal creation failed: {minimal_error}")
                    raise minimal_error
            
        except Exception as e:
            print(f"Error deploying {prefixed_func_name} to AWS: {str(e)}")
            # Print additional debugging information
            if "RequestEntityTooLargeException" in str(e):
                print(f"Payload too large. SECRET_PAYLOAD size: {len(secret_payload)} bytes")
                print("Consider reducing workflow complexity or using external storage")
            elif "InvalidParameterValueException" in str(e):
                print("Check Lambda configuration parameters (memory, timeout, role)")
            sys.exit(1)


def get_openwhisk_credentials(workflow_data):
    # Get OpenWhisk server configuration from workflow data
    for server_name, server_config in workflow_data['ComputeServers'].items():
        if server_config['FaaSType'].lower() == 'openwhisk':
            return (
                server_config['Endpoint'],
                server_config['Namespace'],
                server_config['SSL'].lower() == 'true'
            )
    
    print("Error: No OpenWhisk server configuration found in workflow data")
    sys.exit(1)

def deploy_to_ow(workflow_data):
    # Get OpenWhisk credentials
    api_host, namespace, ssl = get_openwhisk_credentials(workflow_data)
    
    # Get the workflow name for prefixing
    workflow_name = workflow_data.get('WorkflowName', 'default')
    json_prefix = workflow_name
    
    # Filter actions that should be deployed to OpenWhisk
    ow_actions = {}
    for action_name, action_data in workflow_data['ActionList'].items():
        server_name = action_data['FaaSServer']
        server_config = workflow_data['ComputeServers'][server_name]
        faas_type = server_config['FaaSType'].lower()
        if faas_type in ['openwhisk', 'open_whisk', 'ow']:
            ow_actions[action_name] = action_data
    
    if not ow_actions:
        print("No actions found for OpenWhisk deployment")
        return

    
    # Set up wsk properties
    subprocess.run(f"wsk property set --apihost {api_host}", shell=True)
    
    # Set authentication using API key from environment variable
    ow_api_key = os.getenv('OW_API_KEY')
    if ow_api_key:
        subprocess.run(f"wsk property set --auth {ow_api_key}", shell=True)
        print("Using OpenWhisk with API key authentication")
    else:
        print("Using OpenWhisk without authentication")
    
    # Always use insecure flag to bypass certificate issues
    subprocess.run("wsk property set --insecure", shell=True)
    
    # Set environment variable to handle certificate issue
    env = os.environ.copy()
    env['GODEBUG'] = 'x509ignoreCN=0'
    
    # Process each action in the workflow
    for action_name, action_data in ow_actions.items():
        try:
            actual_func_name = action_data['FunctionName']
            
            # Create prefixed function name using workflow_name-action_name format
            prefixed_func_name = f"{json_prefix}-{action_name}"
            
            # Create or update OpenWhisk action using wsk CLI
            try:
                # First check if action exists (add --insecure flag)
                check_cmd = f"wsk action get {prefixed_func_name} --insecure >/dev/null 2>&1"
                exists = subprocess.run(check_cmd, shell=True, env=env).returncode == 0
                
                # Get container image, with fallback to default
                container_image = workflow_data.get('ActionContainers', {}).get(action_name, 'ghcr.io/faasr/openwhisk-tidyverse')
                
                if exists:
                    # Update existing action (add --insecure flag)
                    cmd = f"wsk action update {prefixed_func_name} --docker {container_image} --insecure"
                else:
                    # Create new action (add --insecure flag)
                    cmd = f"wsk action create {prefixed_func_name} --docker {container_image} --insecure"
                
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
                
                if result.returncode != 0:
                    raise Exception(f"Failed to {'update' if exists else 'create'} action: {result.stderr}")
                
                print(f"Successfully deployed {prefixed_func_name} to OpenWhisk")
                
            except Exception as e:
                print(f"Error deploying {prefixed_func_name} to OpenWhisk: {str(e)}")
                sys.exit(1)
                
        except Exception as e:
            print(f"Error processing {prefixed_func_name}: {str(e)}")
            sys.exit(1)

def main():
    args = parse_arguments()
    workflow_data = read_workflow_file(args.workflow_file)
    
    # Store the workflow file path in the workflow data
    workflow_data['_workflow_file'] = args.workflow_file
    
    # Validate workflow for cycles and unreachable states
    print("Validating workflow for cycles and unreachable states...")
    try:
        check_dag(workflow_data)
        print("✓ Workflow validation passed - no cycles or unreachable states found")
    except SystemExit:
        print("✗ Workflow validation failed - check logs for details")
        sys.exit(1)
    
    # Get all unique FaaSTypes from workflow data
    faas_types = set()
    for server in workflow_data.get('ComputeServers', {}).values():
        if 'FaaSType' in server:
            faas_types.add(server['FaaSType'].lower())
    
    if not faas_types:
        print("Error: No FaaSType found in workflow file")
        sys.exit(1)
    
    print(f"Found FaaS platforms: {', '.join(faas_types)}")
    
    # Deploy to each platform found
    for faas_type in faas_types:
        print(f"\nDeploying to {faas_type}...")
        if faas_type in ['lambda', 'aws_lambda', 'aws']:
            deploy_to_aws(workflow_data)
        elif faas_type in ['githubactions', 'github_actions', 'github']:
            deploy_to_github(workflow_data)
        elif faas_type in ['openwhisk', 'open_whisk', 'ow']:
            deploy_to_ow(workflow_data)
        else:
            print(f"Warning: Unknown FaaSType '{faas_type}' - skipping")
    

if __name__ == '__main__':
    main() 
