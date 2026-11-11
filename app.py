from datetime import date, time

import streamlit as st
from pawpal_system import Owner, Pet, Task, Priority, Frequency, Scheduler, TIME_BLOCKS

st.set_page_config(page_title="PawPal+", page_icon="🐾", layout="centered")

st.title("🐾 PawPal+")

st.markdown(
    """
**PawPal+** is a pet care planning assistant. Add your pets and the time you have available,
add their care tasks, and PawPal+ builds a daily plan that fits the most important tasks
into your day and explains why each one was placed where it was.
"""
)

st.divider()

st.subheader("Owner and pets")
owner_name = st.text_input("Owner name", value="Jordan")
pet_name = st.text_input("Pet name", value="Mochi")
species = st.selectbox("Species", ["dog", "cat", "other"])

if "owner" not in st.session_state:
    st.session_state.owner = Owner(owner_name)

# Always update the owner_name in the case the user renames it
st.session_state.owner.name = owner_name

if st.button("Add a Pet"):
    try:
        st.session_state.owner.add_pet(Pet(pet_name, species))
        st.success(f"Added {pet_name}")
    except ValueError as e:
        st.error(str(e))

# The owner's pets list is the single source of truth for which pets exist
pet_names = []
for pet in st.session_state.owner.pets:
    pet_names.append(pet.name)

st.markdown("### Available time")
st.caption("How many minutes you have in each part of the day. The scheduler only fits tasks into this time.")

DEFAULT_MINUTES = {"morning": 60, "afternoon": 0, "evening": 60}
time_columns = st.columns(len(TIME_BLOCKS))
for time_column, block in zip(time_columns, TIME_BLOCKS):
    with time_column:
        minutes = st.number_input(
            f"{block.capitalize()} (minutes)",
            min_value=0,
            max_value=720,
            value=DEFAULT_MINUTES[block],
            key=f"minutes_{block}",
        )
    # Always update the owner's constraints so the scheduler sees the latest values
    st.session_state.owner.constraints[block] = minutes

st.markdown("### Tasks")
st.caption("Add care tasks for your pets. An optional start time lets PawPal+ warn you about overlapping tasks.")

col1, col2, col3, col4, col5, col6 = st.columns(6)
with col1:
    title = st.text_input("Task title", value="Morning walk")
with col2:
    description = st.text_input("Task Description", value="walk in the local park nearby")
with col3:  
    duration = st.number_input("Duration (minutes)", min_value=1, max_value=240, value=20)
with col4:
    priority = st.selectbox("Priority", [Priority.LOW, Priority.MEDIUM, Priority.HIGH], index=2)
with col5:
    preferred_time = st.selectbox("Preferred Time", ["morning", "afternoon", "evening"], index=1)
with col6:
    frequency = st.selectbox("Frequency", [Frequency.ONCE, Frequency.DAILY, Frequency.WEEKLY], index=1)

date_col, start_check_col, start_time_col = st.columns(3)
with date_col:
    due_date = st.date_input("Due date", value=date.today())
with start_check_col:
    # Start time is optional; only timed tasks are checked for conflicts
    use_start_time = st.checkbox("Set a start time", value=False)
with start_time_col:
    start_time_value = st.time_input(
        "Start time",
        value=time(8, 0),
        disabled=not use_start_time,
    )

if use_start_time:
    start_time = start_time_value.strftime("%H:%M")
else:
    start_time = None

if not pet_names:
    st.info("Add a pet before adding tasks.")
else:
    selected_pet_name = st.selectbox("Assign task to pet", pet_names)

    if st.button("Add task"):
        try:
            task = Task(
                title,
                description,
                duration,
                priority,
                preferred_time,
                frequency,
                due_date=due_date,
                start_time=start_time,
            )
            # Find the selected pet on the owner and add the task to it
            for pet in st.session_state.owner.pets:
                if pet.name == selected_pet_name:
                    pet.create_task(task)
                    break
            st.success(f"Added {title} to {selected_pet_name}")
        except ValueError as e:
            st.error(str(e))

# A a new Scheduler each rerun; the owner in session_state holds all the real data
scheduler = Scheduler(st.session_state.owner)

st.markdown("### Current tasks")
st.caption("Sorted by time of day, then start time, priority and duration.")

# Conflicts cover all incomplete timed tasks, so they ignore the filters below
conflict_warnings = scheduler.detect_conflicts()
if len(conflict_warnings) > 0:
    st.markdown("**Schedule conflicts found**")
    for conflict_warning in conflict_warnings:
        st.warning(conflict_warning)
    st.caption("Tasks overlap when their start time and duration run into each other. Change a start time to fix it.")

filter_pet_col, filter_status_col = st.columns(2)
with filter_pet_col:
    pet_filter = st.selectbox("Show pet", ["All pets"] + pet_names)
with filter_status_col:
    status_filter = st.selectbox("Show status", ["All", "Incomplete", "Completed"])

if pet_filter == "All pets":
    pet_filter_name = None
else:
    pet_filter_name = pet_filter

if status_filter == "Incomplete":
    completed_filter = False
elif status_filter == "Completed":
    completed_filter = True
else:
    completed_filter = None

matching_tasks = scheduler.filter_tasks(
    pet_name=pet_filter_name,
    completed=completed_filter,
)
visible_tasks = scheduler.sort_by_time(matching_tasks)

if len(st.session_state.owner.get_all_tasks()) == 0:
    st.info("No tasks yet. Add one above.")
elif len(visible_tasks) == 0:
    st.info("No tasks match these filters.")
else:
    # Build readable rows so the table shows names and labels instead of raw values
    rows = []
    for task in visible_tasks:
        if task.completion_status:
            done_label = "✅"
        else:
            done_label = "⬜"
        if task.start_time is None:
            start_label = "-"
        else:
            start_label = task.start_time
        rows.append({
            "Pet": task.pet_name,
            "Task": task.name,
            "Description": task.description,
            "Minutes": task.duration,
            "Priority": task.priority.name,
            "Time": task.preferred_time,
            "Start": start_label,
            "Due": task.due_date.isoformat(),
            "Frequency": task.frequency.value,
            "Done": done_label,
        })
    st.table(rows)

st.markdown("### Mark a task complete")

# Shown after the rerun that follows a completion, since the table above was already drawn
completion_message = st.session_state.pop("completion_message", None)
if completion_message:
    st.success(completion_message)

# Task names are unique among a pet's incomplete tasks, so pet + name picks exactly one task
incomplete_by_label = {}
for task in scheduler.sort_by_time():
    label = f"{task.pet_name}: {task.name} (due {task.due_date.isoformat()})"
    incomplete_by_label[label] = task

if len(incomplete_by_label) == 0:
    st.info("No incomplete tasks.")
else:
    chosen_label = st.selectbox("Task to complete", list(incomplete_by_label))
    if st.button("Mark complete"):
        chosen_task = incomplete_by_label[chosen_label]
        next_task = scheduler.mark_task_complete(chosen_task.pet_name, chosen_task.name)
        if next_task is None:
            st.session_state.completion_message = (
                f"Completed {chosen_task.name} for {chosen_task.pet_name}."
            )
        else:
            st.session_state.completion_message = (
                f"Completed {chosen_task.name} for {chosen_task.pet_name}. "
                f"Next {next_task.frequency.value} occurrence created, due {next_task.due_date.isoformat()}."
            )
        st.rerun()

st.markdown("### Remove a task")
st.caption("Delete a task you added by mistake, for example one that causes a schedule conflict.")

removal_message = st.session_state.pop("removal_message", None)
if removal_message:
    st.success(removal_message)

if len(incomplete_by_label) == 0:
    st.info("No incomplete tasks to remove.")
else:
    label_to_remove = st.selectbox("Task to remove", list(incomplete_by_label))
    if st.button("Remove task"):
        task_to_remove = incomplete_by_label[label_to_remove]
        for pet in st.session_state.owner.pets:
            if pet.name == task_to_remove.pet_name:
                pet.remove_task(task_to_remove.name)
                break
        st.session_state.removal_message = (
            f"Removed {task_to_remove.name} from {task_to_remove.pet_name}."
        )
        st.rerun()

st.divider()

st.subheader("Build Schedule")
st.caption(
    "Fits incomplete tasks due on or before the chosen date into the available time above, "
    "highest priority first."
)
plan_date = st.date_input("Plan for date", value=date.today())

if st.button("Generate schedule"):
    tasks_for_date = []
    for task in scheduler.filter_tasks(completed=False):
        if task.due_date <= plan_date:
            tasks_for_date.append(task)

    if len(tasks_for_date) == 0:
        st.info(f"No incomplete tasks due on or before {plan_date.isoformat()}.")
    else:
        scheduler.generate_daily_plan(plan_date)
        scheduler.generate_explanation_of_plan()

        for block in TIME_BLOCKS:
            block_tasks = scheduler.plan[block]
            minutes_used = 0
            for task in block_tasks:
                minutes_used += task.duration
            minutes_available = st.session_state.owner.constraints[block]

            st.markdown(f"**{block.capitalize()}** ({minutes_used}/{minutes_available} min used)")
            if len(block_tasks) == 0:
                st.caption("Nothing scheduled")
            for task in block_tasks:
                reason = scheduler.placement_reasons[(task.pet_name, task.name)]
                st.write(f"- {task.pet_name}: {task.name} ({task.duration} min) - {reason}")

        if len(scheduler.skipped_tasks) > 0:
            st.markdown("**Skipped tasks**")
            for (skipped_pet, skipped_task), reason in scheduler.skipped_tasks.items():
                st.warning(f"{skipped_pet}: {skipped_task} - {reason}")

        with st.expander("Plain-text explanation of the plan"):
            st.text(scheduler.explanation_of_plan)
