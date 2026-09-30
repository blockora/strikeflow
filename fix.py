with open('app/main.py', 'r') as f:
    content = f.read()

# Count braces
open_braces = content.count('{')
close_braces = content.count('}')
print('Open:', open_braces, 'Close:', close_braces)

# Find positions of '}' 
positions = []
for i in range(len(content)):
    if content[i] == '}':
        positions.append(i)

print('Positions of }:', positions)

# Remove the last '}'
if positions:
    last_pos = positions[-1]
    new_content = content[:last_pos] + content[last_pos+1:]
    
    with open('app/main.py', 'w') as f:
        f.write(new_content)
    
    print('Now open:', new_content.count('{'), 'close:', new_content.count('}'))
