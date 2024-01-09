import json
import pandas as pd
import plotly.figure_factory as ff

# Load the JSON data
file_path = './stack_events.json'
with open(file_path, 'r') as file:
    data = json.load(file)

# Extracting events
events = data['StackEvents']

# Prepare the DataFrame
df_events = pd.DataFrame(events)

df_events = df_events.sort_values("Timestamp")
g_start = df_events["Timestamp"][len(df_events)-1]
g_end = df_events["Timestamp"][0]

# Selecting relevant columns and converting Timestamp to datetime
df_gantt = df_events[['LogicalResourceId', 'Timestamp', 'ResourceStatus']]
df_gantt['Timestamp'] = pd.to_datetime(df_gantt['Timestamp'], format="ISO8601")

# Processing data for Gantt chart
# We need to find the start and end time for each status of each resource
gantt_data = []
resources = df_gantt['LogicalResourceId'].unique()

for resource in resources:
    resource_data = df_gantt[df_gantt['LogicalResourceId'] == resource]
    for status in resource_data['ResourceStatus'].unique():
        status_data = resource_data[resource_data['ResourceStatus'] == status]
        start_time = status_data['Timestamp'].min()
        end_time = status_data['Timestamp'].max()
        if start_time != end_time:  # Avoid adding data where start and end time are same
            gantt_data.append(dict(Task=resource, Start=start_time, Finish=end_time, Status=status))

# Creating the Gantt chart
fig = ff.create_gantt(gantt_data, colors=None, index_col='Status', show_colorbar=True, group_tasks=True)

# Code Explanation
# 1. The data from the JSON file is loaded into a DataFrame.
# 2. We filter out the necessary columns - LogicalResourceId, Timestamp, and ResourceStatus.
# 3. We convert the 'Timestamp' column to datetime for proper plotting.
# 4. We then iterate over each unique resource and its statuses to find the start and end times for each status.
# 5. These start and end times are used to create data points for the Gantt chart.
# 6. Finally, we use Plotly's `create_gantt` function to create and display the Gantt chart.

# Please note that we filter out data points where the start and end times are the same.

# Return the plotly figure object for further modifications or direct plotting
# Increase the height of the figure. Adjust the value as needed.

fig.update_xaxes(
    tickformat='%Y-%m-%d %H:%M',
    tickangle=-45
)

fig.update_xaxes(range=[g_start, g_end])

fig.update_layout(height=(25 * len(resources)))

# Increase the font size for the y-axis labels
fig.update_yaxes(tickfont=dict(size=12))

# Adjust margins if necessary
fig.update_layout(margin=dict(l=100, r=20, t=20, b=20))
fig.show()

