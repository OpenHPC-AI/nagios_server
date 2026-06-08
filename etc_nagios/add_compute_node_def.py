import os
import sys
import subprocess  # Ensure subprocess is imported
import readline  # Import the readline module for enhanced input

# Function to run shell commands
def run_command(command):
    try:
        result = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.decode().strip()
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {e}")
        sys.exit(1)

# Function to define node prefix based on the node number and max digit count
def define_prefix(node_number, prefix, max_digit_count):
    formatted_node_number = str(node_number).zfill(max_digit_count)
    return f"{prefix}{formatted_node_number}"

# Function to get the maximum number of digits needed for node numbering
def get_max_digit_count(start_node_no, last_node_no):
    max_digit_count = len(str(last_node_no))  # Get the length of the last node number
    return max(max_digit_count, 3)  # Ensure at least 3 digits

# Function to get user input
def get_user_input(prompt):
    try:
        return input(prompt)
    except EOFError:
        print("Error: End of input encountered!")
        sys.exit(1)



def main():
    # Check if the user provided enough arguments
    if len(sys.argv) < 3:
        print("Usage: python3 add.py <hosts_cfg_path> <template_name>")
        sys.exit(1)

    # Get the variables from the command line arguments
    hosts_cfg_path = sys.argv[1]
    template_name = sys.argv[2]

    # Get user inputs
    subnet_prefix = int(get_user_input("Enter the Subnet Prefix of Network (Valid range: 18-24): "))
    if subnet_prefix < 18 or subnet_prefix > 24:
        print("Invalid subnet prefix. Please enter a value between 18 and 24.")
        sys.exit(1)

    node_type = get_user_input("Enter the Node Type to Add in Host Groups (e.g., compute, hm, gpu): ")
    pv_net_address = get_user_input(f"Enter Private Network Address (Starting Pvt_IP Address of {node_type} node): ")
    prefix = get_user_input(f"Enter the Prefix Value for {node_type} node (e.g., rbcn, rpcn, cn): ")
    start_node_no = int(get_user_input(f"Enter the Start {node_type} node number: "))
    last_node_no = int(get_user_input(f"Enter the Last {node_type} node number: "))

    # Calculate variables for IP addressing
    max_digit_count = get_max_digit_count(start_node_no, last_node_no)
    pvt_ip_network_var = ".".join(pv_net_address.split('.')[:3])
    pvt_ip_fourth_octet = int(pv_net_address.split('.')[3])

    # Open the file in append mode
    try:
        with open(hosts_cfg_path, "a") as hosts_cfg:
            # Append hostgroup definition
            hosts_cfg.write(f"\ndefine hostgroup {{\n")
            hosts_cfg.write(f"    hostgroup_name {node_type}\n")
            hosts_cfg.write(f"    alias {node_type} nodes\n")

            # Generate members list
            members = [
                define_prefix(node, prefix, max_digit_count)
                for node in range(start_node_no, last_node_no + 1)
            ]
            members_str = ",".join(members)
            hosts_cfg.write(f"    members {members_str}\n")
            hosts_cfg.write(f"}}\n")

            # Append host definitions
            for node_number in range(start_node_no, last_node_no + 1):
                node_name = define_prefix(node_number, prefix, max_digit_count)
                node_ip = f"{pvt_ip_network_var}.{pvt_ip_fourth_octet}"
                pvt_ip_fourth_octet += 1  # Increment IP for each node

                hosts_cfg.write(f"\ndefine host{{\n")
                hosts_cfg.write(f"    use {template_name}\n")
                hosts_cfg.write(f"    host_name {node_name}\n")
                hosts_cfg.write(f"    alias {node_name}\n")
                hosts_cfg.write(f"    address {node_ip}\n")
                hosts_cfg.write(f"}}\n")
        print(f"Configuration successfully appended to {hosts_cfg_path}")

    except FileNotFoundError:
        print(f"Error: The file {hosts_cfg_path} was not found.")
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
