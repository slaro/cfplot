#!/usr/bin/env python
import boto3
import plotly.graph_objects as go
from collections import OrderedDict
import fire
import logging
from typing import List, Dict, Tuple, Set
from datetime import datetime

# Constants
SECONDS_IN_HOUR = 3600
SECONDS_IN_MINUTE = 60
DEFAULT_PROFILE = "default"
DEFAULT_REGION = "us-east-2"
DEFAULT_FONT = {"family": "Open Sans, light", "color": "black", "size": 14}
COLORS = {
    "stack": {
        "main": "#2E86C1",        # Strong blue for main stack
        "nested": "#3498DB"       # Lighter blue for nested stacks
    },
    "resource": {
        "compute": "#27AE60",     # Green for compute resources (EC2, Lambda)
        "storage": "#8E44AD",     # Purple for storage (S3, EFS)
        "network": "#E67E22",     # Orange for network resources
        "security": "#C0392B",    # Red for security resources
        "other": "#7F8C8D"        # Gray for other resources
    },
    "waiting": "#ECF0F1"          # Light gray for waiting periods
}

# Initialize a module-level logger
logger = logging.getLogger("cfplot_logger")

def setup_logging(debug: bool) -> None:
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False  # Prevents the logger from propagating to the root logger

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%b %d %H:%M:%S'))
    
    logger.addHandler(handler)

def format_time_from_seconds(seconds: int) -> str:
    hours, remainder = divmod(seconds, SECONDS_IN_HOUR)
    minutes, seconds = divmod(remainder, SECONDS_IN_MINUTE)
    return f"{hours:02}:{minutes:02}:{seconds:02}"

def get_resource_category(resource_type: str) -> str:
    """
    Determine the category of a resource based on its type
    """
    compute_resources = {"AWS::EC2::", "AWS::Lambda::", "AWS::AutoScaling::"}
    storage_resources = {"AWS::S3::", "AWS::EFS::", "AWS::DynamoDB::", "AWS::RDS::"}
    network_resources = {"AWS::EC2::VPC", "AWS::EC2::Subnet", "AWS::EC2::RouteTable", 
                        "AWS::EC2::SecurityGroup", "AWS::ElasticLoadBalancing::"}
    security_resources = {"AWS::IAM::", "AWS::KMS::", "AWS::SecretsManager::"}
    
    if any(resource_type.startswith(r) for r in compute_resources):
        return "compute"
    elif any(resource_type.startswith(r) for r in storage_resources):
        return "storage"
    elif any(resource_type.startswith(r) for r in network_resources):
        return "network"
    elif any(resource_type.startswith(r) for r in security_resources):
        return "security"
    return "other"

def get_stack_creation_events(stackname: str, cf_client) -> Tuple[List[Dict], Dict[str, str], datetime]:
    """
    Get initial creation events for a single stack and identify nested stacks
    """
    paginator = cf_client.get_paginator("describe_stack_events")
    all_events = []
    for page in paginator.paginate(StackName=stackname):
        all_events.extend(page["StackEvents"])
    
    # Sort chronologically
    all_events.sort(key=lambda x: x["Timestamp"])
    
    # Debug log the first few events
    logger.debug(f"First 3 events for stack {stackname}:")
    for event in all_events[:3]:
        logger.debug(f"Event: Status={event['ResourceStatus']}, "
                     f"Type={event['ResourceType']}, "
                     f"LogicalId={event['LogicalResourceId']}, "
                     f"Reason={event.get('ResourceStatusReason', 'No reason')}")
    
    # For nested stacks, find the first CREATE_IN_PROGRESS and CREATE_COMPLETE events
    start_event = next(
        (e for e in all_events 
         if e["ResourceStatus"] == "CREATE_IN_PROGRESS" 
         and e["ResourceType"] == "AWS::CloudFormation::Stack"
         and e.get("ResourceStatusReason", "") == "User Initiated"),
        None
    )
    
    # Get the actual logical ID from the stack events
    stack_logical_id = next(
        (e["LogicalResourceId"] for e in all_events 
         if e["ResourceType"] == "AWS::CloudFormation::Stack"),
        stackname.split('/')[-1]
    )
    
    complete_event = next(
        (e for e in all_events 
         if e["ResourceStatus"] == "CREATE_COMPLETE"
         and e["ResourceType"] == "AWS::CloudFormation::Stack"
         and e["LogicalResourceId"] == stack_logical_id),
        None
    )
    
    if not start_event or not complete_event:
        logger.warning(f"Could not find start or complete event for stack: {stackname}")
        logger.warning(f"Looking for logical ID: {stack_logical_id}")
        logger.warning(f"Total events found: {len(all_events)}")
        if all_events:
            logger.warning("First event:")
            logger.warning(f"Status={all_events[0]['ResourceStatus']}, "
                           f"Type={all_events[0]['ResourceType']}, "
                           f"LogicalId={all_events[0]['LogicalResourceId']}")
            logger.warning("Last event:")
            logger.warning(f"Status={all_events[-1]['ResourceStatus']}, "
                           f"Type={all_events[-1]['ResourceType']}, "
                           f"LogicalId={all_events[-1]['LogicalResourceId']}")
        return [], {}, None
    
    start_time = start_event["Timestamp"]
    complete_time = complete_event["Timestamp"]
    
    logger.info(f"Found valid start/complete events for {stackname}")
    logger.debug(f"Start: {start_time}, Complete: {complete_time}")
    
    # Track nested stacks and their creation times
    nested_stacks = {}
    creation_events = []
    
    for event in all_events:
        # Only include events between stack start and complete
        if start_time <= event["Timestamp"] <= complete_time:
            creation_events.append(event)
            
            # Track nested stack creation
            if (event["ResourceType"] == "AWS::CloudFormation::Stack" and 
                event["PhysicalResourceId"] != stackname and
                event["ResourceStatus"] == "CREATE_IN_PROGRESS" and
                event["PhysicalResourceId"]):
                nested_stacks[event["PhysicalResourceId"]] = event["Timestamp"]
                logger.debug(f"Detected nested stack: {event['PhysicalResourceId']} "
                             f"with LogicalId: {event['LogicalResourceId']} "
                             f"at {event['Timestamp']}")
    
    return creation_events, nested_stacks, complete_time

def retrieve_cf_events(stackname: str, profile: str, region: str, root_complete_time: datetime = None, processed_stacks: Set[str] = None) -> List[Dict]:
    """
    Retrieve all events including nested stacks for initial creation only
    """
    if not stackname:
        logger.error("Stack name is required to retrieve events.")
        return []
        
    if processed_stacks is None:
        processed_stacks = set()
        
    if stackname in processed_stacks:
        logger.info(f"Stack {stackname} has already been processed.")
        return []
        
    processed_stacks.add(stackname)
    
    logger.info(f"Retrieving events for stack: {stackname}")
    session = boto3.session.Session(profile_name=profile, region_name=region)
    cf_client = session.client("cloudformation")
    
    # Get events for this stack and identify nested stacks
    stack_events, nested_stacks, stack_complete_time = get_stack_creation_events(stackname, cf_client)
    
    # For root stack, establish the completion time
    complete_time = root_complete_time or stack_complete_time
    if not complete_time:
        logger.warning(f"No completion time found for stack: {stackname}")
        return []
    
    all_events = stack_events.copy()
    
    # Process nested stacks in creation order, but only if created before root stack completed
    for nested_stack, creation_time in sorted(nested_stacks.items(), key=lambda x: x[1]):
        if nested_stack and creation_time <= complete_time:  # Only process stacks created before root completion
            logger.debug(f"Processing nested stack: {nested_stack} (created at {creation_time})")
            try:
                nested_events = retrieve_cf_events(
                    stackname=nested_stack,
                    profile=profile,
                    region=region,
                    root_complete_time=complete_time,  # Pass down the root completion time
                    processed_stacks=processed_stacks
                )
                logger.debug(f"Retrieved {len(nested_events)} events from nested stack: {nested_stack}")
                all_events.extend(nested_events)
            except Exception as e:
                logger.warning(f"Could not retrieve events for nested stack {nested_stack}: {str(e)}")
    
    logger.info(f"Total events for stack {stackname}: {len(all_events)}")
    return all_events

def construct_event_trace(start_time, data, event, is_total=False):
    """
    Construct waterfall trace for a single resource
    """
    is_stack = event["ResourceType"] == "AWS::CloudFormation::Stack"
    is_main_stack = is_stack and event["StackName"] == event["LogicalResourceId"]
    resource_category = get_resource_category(event["ResourceType"])
    
    trace = {
        "x": [],
        "y": [[], []],
        "text": [],
        "textfont": DEFAULT_FONT,
        "textposition": "outside",
        "width": 0.6,  # Reduced from 0.8 to create more spacing
        "base": (data["identified"] - start_time).seconds,
        "measure": [],
        "increasing": {
            "marker": {
                "color": COLORS["stack"]["main"] if is_main_stack else
                        COLORS["stack"]["nested"] if is_stack else
                        COLORS["resource"][resource_category]
            }
        },
        "decreasing": {"marker": {"color": COLORS["waiting"]}}
    }
    update_trace(event, trace, is_total, data)
    return trace

def update_trace(event, trace, is_total, data):
    """
    Update trace with timing information
    """
    trace["y"][0].append(event["StackName"])
    trace["y"][1].append(event["LogicalResourceId"])
    
    if is_total:
        trace["x"].append(0)
        trace["measure"].append("relative")
        trace["text"].append("")
        trace["text"].append(format_time_from_seconds(data["duration"].seconds))
    else:
        # Add waiting time segment (identification to start)
        if data["duration_i2s"].seconds > 0:
            trace["x"].append(data["duration_i2s"].seconds)
            trace["measure"].append("relative")
            trace["text"].append("")
            trace["y"][0].append(event["StackName"])
            trace["y"][1].append(event["LogicalResourceId"])
        
        # Add creation time segment (start to end)
        trace["x"].append(data["duration_s2e"].seconds)
        trace["measure"].append("relative")
        trace["text"].append(format_time_from_seconds(data["duration"].seconds))

def main(stackname: str, profile: str = DEFAULT_PROFILE, region: str = DEFAULT_REGION, debug: bool = False) -> None:
    setup_logging(debug)
    logger.info(f"Starting retrieval of events for stack: {stackname}")
    data = OrderedDict()
    fig = go.Figure()
    try:
        events = retrieve_cf_events(stackname=stackname, profile=profile, region=region)
        if not events:
            logger.error("No events found for the stack.")
            return
        start_time = events[0]["Timestamp"]
        process_events(events, start_time, data, fig)
        display_figure(fig, data, events, stackname)
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")

def process_events(events, start_time, data, fig):
    """
    Process events and create waterfall traces
    """
    # First pass: collect all timing data
    for event in events:
        update_data_for_event(event, data)
    
    # Track which stacks we've already processed
    processed_stacks = set()
    traces_created = 0
    
    # Second pass: create traces for completed resources
    for event in events:
        stack_name = event["StackName"]
        logical_id = event["LogicalResourceId"]
        
        # Create a unique identifier for this stack
        stack_identifier = f"{stack_name}/{logical_id}"
        
        if (event["ResourceStatus"] == "CREATE_COMPLETE" and 
            stack_name in data and 
            logical_id in data[stack_name] and 
            data[stack_name][logical_id]["duration"] is not None):
            
            # Skip if we've already processed this stack
            if stack_identifier in processed_stacks:
                logger.debug(f"Skipping already processed stack: {stack_identifier}")
                continue
            
            # Skip root stack self-reference
            if (event["ResourceType"] == "AWS::CloudFormation::Stack" and 
                stack_name == logical_id):
                logger.debug(f"Skipping root stack self-reference: {stack_name}")
                continue
            
            trace = construct_event_trace(
                start_time=start_time,
                data=data[stack_name][logical_id],
                event=event
            )
            fig.add_trace(go.Waterfall(orientation="h", **trace))
            traces_created += 1
            
            # Mark this stack as processed
            processed_stacks.add(stack_identifier)
            
            if event["ResourceType"] == "AWS::CloudFormation::Stack":
                logger.debug(f"Created trace for stack: {logical_id}")
    
    logger.info(f"Created {traces_created} traces for visualization")

def update_data_for_event(event, data):
    """
    Update the data structure with event information for waterfall visualization
    """
    stack_name = event["StackName"]
    logical_resource_id = event["LogicalResourceId"]
    resource_status = event["ResourceStatus"]
    resource_status_reason = event.get("ResourceStatusReason", "").lower()
    timestamp = event["Timestamp"]

    # Initialize stack data if needed
    if stack_name not in data:
        data[stack_name] = {}

    # Initialize resource data if needed
    if logical_resource_id not in data[stack_name]:
        data[stack_name][logical_resource_id] = {
            "identified": None,  # When resource is first seen
            "start": None,      # When creation actually starts
            "end": None,        # When creation completes
            "duration": None,   # Total time from identification to completion
            "duration_i2s": None,  # Time from identification to start
            "duration_s2e": None   # Time from start to completion
        }

    resource_data = data[stack_name][logical_resource_id]
    
    # Update timestamps based on event type and status
    if resource_status == "CREATE_IN_PROGRESS":
        if resource_status_reason == "user initiated":
            # Stack creation initiation
            resource_data["identified"] = timestamp
            resource_data["start"] = timestamp
        elif resource_status_reason == "resource creation initiated":
            # Resource creation actually starting
            if resource_data["identified"] is None:
                resource_data["identified"] = timestamp
            resource_data["start"] = timestamp
        else:
            # First time seeing this resource
            if resource_data["identified"] is None:
                resource_data["identified"] = timestamp
                
    elif resource_status == "CREATE_COMPLETE":
        # Resource creation finished
        resource_data["end"] = timestamp
        
        # Calculate durations only when we have all necessary timestamps
        if resource_data["identified"] and resource_data["start"] and resource_data["end"]:
            resource_data["duration_i2s"] = resource_data["start"] - resource_data["identified"]
            resource_data["duration_s2e"] = resource_data["end"] - resource_data["start"]
            resource_data["duration"] = resource_data["end"] - resource_data["identified"]

def format_time_for_axis(seconds: float) -> str:
    """
    Format seconds into MM:SS format for axis labels
    """
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02}:{seconds:02}"

def display_figure(fig, data, events, stackname):
    # Calculate total duration in seconds from the first trace's base value
    total_duration = max(trace.base + sum(trace.x) for trace in fig.data)
    
    fig.update_layout(
        title={
            "text": f'<span style="color:#2C3E50">CloudFormation Waterfall - {stackname}<br />'
                   f'<b>Total Time: {format_time_from_seconds(int(total_duration))}</b></span>',
            "font": {"family": "Open Sans, light", "size": 20}
        },
        showlegend=False,
        height=max(len(events) * 8, 400),
        font=DEFAULT_FONT,
        plot_bgcolor="#FFFFFF",
        paper_bgcolor="#FFFFFF",
        margin=dict(l=150, r=50, t=100, b=50),
        bargap=0.15,
        bargroupgap=0.1,
        xaxis=dict(
            gridcolor="#ECEFF1",
            zerolinecolor="#CFD8DC",
            showgrid=True,
            tickmode="array",
            # Generate ticks from 0 to total duration
            tickvals=list(range(0, int(total_duration) + 30, 30)),  # Step by 30 seconds
            ticktext=[format_time_for_axis(t) for t in range(0, int(total_duration) + 30, 30)],
            range=[0, total_duration],  # Set explicit range
            tickformat=None
        ),
        yaxis=dict(
            gridcolor="#ECEFF1",
            zerolinecolor="#CFD8DC",
            showgrid=True,
            linecolor="#2C3E50",
            autorange=True,
            constrain="domain",
            constraintoward="middle",
            tickson="boundaries",
            range=[-0.2, len(events) - 0.2]
            # scaleanchor="x",  # Forces consistent scaling
            # scaleratio=0.3    # Controls aspect ratio of the row
        )
    )
    
    for trace in fig.data:
        # Create meaningful customdata for each bar segment
        customdata = []
        for i in range(len(trace.y[0])):
            resource_type = "Resource"  # Default value
            if hasattr(trace, 'increasing') and 'marker' in trace.increasing:
                color = trace.increasing.marker.color
                if color == COLORS["stack"]["main"]:
                    resource_type = "Main Stack"
                elif color == COLORS["stack"]["nested"]:
                    resource_type = "Nested Stack"
                elif color in COLORS["resource"].values():
                    # Reverse lookup the resource category
                    resource_type = next(
                        (k.title() for k, v in COLORS["resource"].items() if v == color),
                        "Resource"
                    )
            
            duration = format_time_from_seconds(trace.x[i]) if trace.x[i] > 0 else "00:00:00"
            start_time = format_time_from_seconds(trace.base)
            end_time = format_time_from_seconds(trace.base + trace.x[i])
            
            customdata.append([
                resource_type,
                duration,
                start_time,
                end_time,
                trace.y[1][i]  # Logical Resource ID
            ])
        
        trace.customdata = customdata
        trace.hovertemplate = (
            "<b>%{customdata[4]}</b><br>" +  # Logical Resource ID
            "Type: %{customdata[0]}<br>" +   # Resource Type
            "Duration: %{customdata[1]}<br>" +
            "Start: %{customdata[2]}<br>" +
            "End: %{customdata[3]}<extra></extra>"
        )
    
    fig.update_xaxes(
        title="Event Duration (MM:SS)",
        tickangle=-45,
        tickfont=DEFAULT_FONT
    )
    
    fig.update_yaxes(
        title="CloudFormation Resources",
        tickangle=0,
        tickfont=DEFAULT_FONT
    )
    
    fig.update_traces(
        connector_visible=False,
        textfont={"color": "#2C3E50"},
        width=0.6, # This affects how big the duration numbers appear. 0.4 is too small for > 100 resources.
        alignmentgroup="resource",
        offsetgroup="resource"
    )
    
    fig.show()

if __name__ == "__main__":
    fire.Fire(main)
