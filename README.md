# realworld-demo-deploy
*Deploying the demo for [RealWorld](https://github.com/gothinkster/realworld)*

---

**[The demo is deployed on demo.realworld.show](https://demo.realworld.show/)**

---

🐍 The backend is a [KISS in-memory single file](https://github.com/c4ffein/realworld-demo-deploy/blob/master/realworld_dummy_server.py) - deployed on a cheap VPS  
🅰️ The frontend is [the angular frontend maintained by Gérôme Grignon](https://github.com/gothinkster/angular-realworld-example-app) - deployed through [this GitHub Pages Action](https://github.com/c4ffein/realworld-demo-deploy/blob/master/.github/workflows/deploy.yml)

---

**Complete account isolation**

- You can create temporary accounts using any username or email (duplicates are allowed)
- Each account operates in complete isolation, displaying only:
  - The original base dataset
  - Modifications made within that specific account

Changes made by other accounts are never visible to you

### Why this implementation
This approach addresses specific requirements:
- **User isolation**: The API ensures users only see their own posts, solving moderation issues from the previous demo
- **Simplicity**: In-memory operations using native Python data structures are a decent option due to the lack of a need for persistence
- **Cost-effectiveness**: Deployable on really cheap servers
- **Rapid development**: Building without a framework since the scope of this project is limited

### How to run
```bash
# Set environment variables
export PATH_PREFIX=/api  # path starts with /api/
export POPULATE_DEMO_DATA=True  # includes mocked data for the demo
export CLIENT_IP_HEADER=X-Forwarded-For  # use `X-Forwarded-For` to define the ip address of the client
export LOG_FILE=log_files/json_lines.log  # location of the log file (will be rotated)

# Run the server
python3 realworld_dummy_server.py 8080
```

See [the angular frontend README](https://github.com/gothinkster/angular-realworld-example-app) for deployment without using [this GitHub Pages Action](https://github.com/c4ffein/realworld-demo-deploy/blob/master/.github/workflows/deploy.yml)

### Better implementation for Python code
For an example of a Python implementation that actually enforces best-practices (but doesn't enforce session isolation), you can check my [Django Ninja implementation](https://github.com/c4ffein/realworld-django-ninja)  
This project wasn't used as a base as the needs are quite different - a codebase that could resemble the MVP of an early-stage startup VS a demo project with specific constraints

### Future of this project
There is room for improvements, but there are currently no planned evolutions.
Issues are open and will be addressed (either for bug fixes, code cleaning in case this specific implementation would be useful, or requests for additional features)
