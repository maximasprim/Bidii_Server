"""
Starting templates for the built-in performance roles.

Wording comes from the company's own documents where one exists:
  * Credit Risk Analyst   - "JD - Credit Risk Analyst.pdf"
  * Loan Officer          - "Personal Development Record" (appraisal form)
  * Branch Admin          - "Staff Performance Appraisal Form" (admins form)
Branch Manager, Check-off Agent and Debt Recovery have no source document yet:
their KPI sets are PROPOSALS (weights sum to 100) for HR to confirm or edit.
HR Officer, Marketing Manager and Head Office Staff are deliberately empty
skeletons - HR supplies their JDs (upload one in the JD builder).
Every template is a starting point: it is seeded as version 1 and is editable.
"""

from __future__ import annotations

import copy

DEFAULT_COMPETENCIES = [
    {"key": "putting_customers_first", "title": "Putting customers first",
     "description": "Understands the value of profitable customers. Listening to and understanding customer needs. Delivering outstanding customer service."},
    {"key": "performing_through_our_people", "title": "Performing through our people",
     "description": "Motivates people & teams to perform. Values & adapts to different cultures. Recruits high performers. Develops & coaches the team to succeed."},
    {"key": "delivering_results", "title": "Delivering results",
     "description": "Can do. Sets & prioritizes challenging targets. Decisive, makes decisions. Focused, manages own time & other resources. Is cost conscious without reducing profitability and compromising on quality. Manages risks."},
    {"key": "managing_a_changing_environment", "title": "Managing a changing environment",
     "description": "Analytical, simplifies the complex & ambiguous. Thinks laterally & creatively. Displays sound judgment, solves problems."},
    {"key": "making_a_personal_difference", "title": "Making a personal difference",
     "description": "Positive & courageous. Open, trustworthy & trusting. Resilient, takes personal responsibility. Curious, seeks opportunities to learn."},
    {"key": "communicating_for_impact", "title": "Communicating for impact",
     "description": "Communicates with enthusiasm & clarity. Inspires and influences others."},
]


def _resp(title, weight, description, criteria):
    return {"title": title, "weight_pct": weight, "description": description, "criteria": criteria}


def _kpi(key, label, unit, direction, weight, source, frequency, section, description=None):
    return {"key": key, "label": label, "unit": unit, "direction": direction, "weight": weight, "source": source,
            "frequency": frequency, "cap_pct": None, "jd_section": section, "description": description}


def _jd(title, reports_to="", department="", purpose="", responsibilities=None, competencies=None, **extra):
    base = {"title": title, "reports_to": reports_to, "branch": "", "department": department, "grade": "", "date": "",
            "purpose": purpose, "responsibilities": responsibilities or [], "mandates": "", "planning": "",
            "relationships": "", "qualifications": [], "competencies": competencies if competencies is not None else [],
            "approvals": [], "objective_rows": []}
    base.update(extra)
    return base


_SELF = "Self (Character) and People Skills"

_LOAN_OFFICER = {
    "jd": _jd(
        "Loan Officer", "Sales Team Leader", "Commercial",
        "To acquire quality customers and deliver loan disbursement targets while keeping the loan book healthy.",
        [
            _resp("Customer Acquisition", 20, "Acquire quality customers.", ["New customers per month - indicate the number of new customers added every month"]),
            _resp("Sales", 50, "Deliver loan disbursement targets.", ["Total value of loans disbursed per month. Minimum Target as per staff level"]),
            _resp("Loan Quality", 20, "Manage loan book quality to ensure timely repayment and minimize Portfolio at Risk (PAR).", ["30-day PAR < 5%. Indicate your PAR as of close of the appraisal period."]),
            _resp("Customer Retention", 5, "Maintain good customer relations to ensure retention of active customers.", ["No of customers deactivated or balanced off"]),
            _resp(_SELF, 5, "Self (character) and people skills.", ["Integrity, drive, team player"]),
        ],
        objective_rows=["New customers", "Sales", "Quality (PAR)"],
    ),
    "kpis": [
        _kpi("sales", "Sales - loan value disbursed", "kes", "higher", 50, "kpi_entry", "monthly", "Sales", "Total value of loans disbursed per month against the officer's monthly target."),
        _kpi("new_customers", "New customers acquired", "count", "higher", 20, "kpi_entry", "monthly", "Customer Acquisition"),
        _kpi("par30", "Loan quality - 30-day PAR", "percent", "lower", 20, "kpi_entry", "monthly", "Loan Quality", "Target below 5%."),
        _kpi("retention", "Customers deactivated or balanced off", "count", "lower", 5, "kpi_entry", "monthly", "Customer Retention"),
        _kpi("self_character", "Self (character) and people skills", "rating", "higher", 5, "appraisal_rating", "quarterly", _SELF),
    ],
    "chain": ["staff", "manager", "rm_gm", "hr"],
}

# PROPOSAL: Auto Champions book auto loans and manage an auto portfolio; the induction deck gives their sales,
# quality (PAR) and level targets (see the levels), so they share the Loan Officer KPI set until HR says otherwise.
# Portfolio-earnings rules (minimum net portfolio, 0.2%/0.3% on portfolio, repeat loans) belong to the
# incentive scheme, not to this performance score, and are not modelled here.
_AUTO_CHAMPION = copy.deepcopy(_LOAN_OFFICER)
_AUTO_CHAMPION["jd"]["title"] = "Auto Champion"
_AUTO_CHAMPION["jd"]["purpose"] = "To book auto-loan business and manage the auto portfolio within the sales and quality targets for the level."

_BRANCH_ADMIN = {
    "jd": _jd(
        "Branch Admin", "STL / Branch Manager", "Commercial",
        "To keep the branch office, documents, accounts and customer service running accurately and on time.",
        [
            _resp("Office Management", 20, "Front office duties, office cleanliness & organization, staff welfare.", ["Office should be clean by 8 AM", "100% accounting of welfare cash"]),
            _resp("Document Management", 20, "Filing of documents, timely submission of loan document security & record keeping.", ["100% collection of loan materials & storage in the office cabinet", "Submission of logbooks and title deeds to finance"]),
            _resp("Account Management", 20, "Payment postings, account balancing off, LGF transfer.", ["Same day posting and balancing off of all requests made"]),
            _resp("Customer Experience", 30, "Attend to walk-ins; loan disbursement compliance - calls & taking minutes of the BCC meetings; daily performance report preparation; manage incoming customer communication on the available channels.", ["Prompt attendance & minute taking of all BCC meetings", "Same day compliance", "Reports prepared by 8:30 AM"]),
            _resp("Self", 10, "Character, people skills, team player, discipline.", ["Integrity, drive, team player"]),
        ],
        objective_rows=["Document management", "Account management", "Customer experience", "Sales"],
    ),
    "kpis": [
        _kpi("office_management", "Office Management", "rating", "higher", 20, "appraisal_rating", "quarterly", "Office Management"),
        _kpi("document_management", "Document Management", "rating", "higher", 20, "appraisal_rating", "quarterly", "Document Management"),
        _kpi("account_management", "Account Management", "rating", "higher", 20, "appraisal_rating", "quarterly", "Account Management"),
        _kpi("customer_experience", "Customer Experience", "rating", "higher", 30, "appraisal_rating", "quarterly", "Customer Experience"),
        _kpi("self", "Self - character, people skills, discipline", "rating", "higher", 10, "appraisal_rating", "quarterly", "Self"),
    ],
    # The admins form has no HR step: appraisee -> STL/BM -> CSM -> GM/RM (CEO if applicable).
    "chain": ["staff", "manager", "csm", "rm_gm"],
}

_CREDIT_RISK_ANALYST = {
    "jd": _jd(
        "Credit Risk Analyst", "Head of Risk and Compliance", "Commercial",
        "To safeguard Bidii Credit's loan portfolio and drive sustainable financial growth by evaluating credit applications, monitoring portfolio performance, and implementing robust risk mitigation strategies across the Kenyan branch network.",
        [
            _resp("Credit Evaluation and Underwriting", 30,
                  "1) Analyze credit data and financial statements of loan applicants to assess creditworthiness and repayment capacity. 2) Review and evaluate high-value or complex loan applications in accordance with Bidii Credit's risk policies.",
                  ["Accuracy and turnaround time of credit application assessments.", "Compliance of evaluated loans with internal risk policies and CBK guidelines."]),
            _resp("Portfolio Monitoring and Risk Modeling", 25,
                  "1) Monitor the overall credit portfolio performance and identify emerging trends, concentrations, or potential defaults. 2) Develop, test, and refine credit scoring models and risk assessment frameworks tailored to the Kenyan lending market.",
                  ["Timely identification of portfolio risk trends and early warning signals.", "Accuracy and predictive power of deployed credit scoring models."]),
            _resp("Portfolio Audits and Compliance", 15,
                  "1) Conduct regular portfolio audits across various branches to ensure adherence to credit policies and regulatory guidelines.",
                  ["Completion of scheduled branch risk audits as per the annual audit plan.", "Reduction in policy non-compliance instances identified during audits."]),
            _resp("Risk Reporting and Committee Support", 15,
                  "1) Prepare comprehensive risk reports and dashboards for senior management and the credit committee.",
                  ["Timely and accurate delivery of monthly risk dashboards and committee reports.", "Clarity and actionability of risk insights provided to senior management."]),
            _resp("Collections Strategy and Cross-Functional Collaboration", 15,
                  "1) Collaborate with the collections and recovery teams to formulate strategies for managing non-performing loans (NPLs).",
                  ["Effective contribution to NPL reduction initiatives and recovery planning.", "Strength of cross-functional alignment between risk, branches, and collections teams."]),
        ],
        competencies=DEFAULT_COMPETENCIES,
        branch="Bidii Headquarters - Nairobi", date="2026",
        mandates="Can approve or reject credit applications up to designated underwriting limits and recommend portfolio exception approvals to the Credit Committee.",
        planning="Responsible for planning periodic branch risk audit schedules, risk assessment model testing timelines, and monthly portfolio review reporting cycles.",
        relationships="Routinely works with branch managers, credit officers, the collections and recovery teams, and executive management.",
        qualifications=[
            "Bachelor's degree in Finance, Economics, Statistics, Actuarial Science, or a related quantitative field.",
            "Minimum of 3 years of experience in credit risk management, financial analysis, or underwriting within the banking or microfinance sector in Kenya.",
            "Proficiency in advanced data analysis tools and financial modeling software (e.g., Excel, SQL, R, or Python).",
            "Deep understanding of Kenyan financial regulations, CBK guidelines, and microfinance lending practices.",
            "Strong analytical mindset with high attention to detail and ability to interpret complex financial data.",
            "Excellent communication and interpersonal skills for working with diverse branch teams and stakeholders.",
            "Professional certifications such as CPA (K), ACCA, or CIFA are an added advantage.",
            "Willingness to travel periodically to Bidii Credit branches across the country for risk audits and training.",
        ],
        approvals=["General Manager - Commercial", "CEO"],
    ),
    "kpis": [
        _kpi("credit_evaluation", "Credit Evaluation and Underwriting", "rating", "higher", 30, "appraisal_rating", "quarterly", "Credit Evaluation and Underwriting"),
        _kpi("portfolio_monitoring", "Portfolio Monitoring and Risk Modeling", "rating", "higher", 25, "appraisal_rating", "quarterly", "Portfolio Monitoring and Risk Modeling"),
        _kpi("portfolio_audits", "Portfolio Audits and Compliance", "rating", "higher", 15, "appraisal_rating", "quarterly", "Portfolio Audits and Compliance"),
        _kpi("risk_reporting", "Risk Reporting and Committee Support", "rating", "higher", 15, "appraisal_rating", "quarterly", "Risk Reporting and Committee Support"),
        _kpi("collections_strategy", "Collections Strategy and Cross-Functional Collaboration", "rating", "higher", 15, "appraisal_rating", "quarterly", "Collections Strategy and Cross-Functional Collaboration"),
    ],
    "chain": ["staff", "manager", "rm_gm", "hr"],
}

_BRANCH_MANAGER = {  # PROPOSAL - no source document
    "jd": _jd(
        "Branch Manager", "Regional Manager", "Commercial",
        "To deliver the branch's disbursement and portfolio-quality targets through a well-coached team.",
        [
            _resp("Branch Sales vs Target", 40, "Branch disbursement against the combined targets of the branch's officers.", ["Total branch disbursement versus the sum of officers' monthly targets (computed from verified figures)."]),
            _resp("Branch Portfolio Quality", 20, "Keep the branch loan book healthy.", ["Branch 30-day PAR against target."]),
            _resp("Team Performance Mix", 15, "Move officers to Meeting Expectation or above.", ["Share of the branch's officers at Meeting Expectation or above."]),
            _resp("Compliance and People Management", 15, "Policy compliance, coaching, timely appraisals and verification of officers' monthly entries.", ["Appraisals and monthly verifications completed on time.", "Policy compliance."]),
            _resp(_SELF, 10, "Self (character) and people skills.", ["Integrity, drive, team player"]),
        ],
    ),
    "kpis": [
        _kpi("branch_sales", "Branch sales vs target", "kes", "higher", 40, "computed", "monthly", "Branch Sales vs Target"),
        _kpi("branch_par30", "Branch 30-day PAR", "percent", "lower", 20, "kpi_entry", "monthly", "Branch Portfolio Quality"),
        _kpi("team_mix", "Officers at Meeting Expectation or above", "percent", "higher", 15, "computed", "quarterly", "Team Performance Mix"),
        _kpi("compliance_people", "Compliance and people management", "rating", "higher", 15, "appraisal_rating", "quarterly", "Compliance and People Management"),
        _kpi("self_character", "Self (character) and people skills", "rating", "higher", 10, "appraisal_rating", "quarterly", _SELF),
    ],
    "chain": ["staff", "manager", "rm_gm", "hr"],
}

_CHECK_OFF = {  # PROPOSAL - modelled on the Check-off sheet (KES disbursed vs a monthly target)
    "jd": _jd(
        "Check-off Agent", "", "Commercial",
        "To grow and maintain check-off (employer-deduction) lending within the agreed targets and quality limits.",
        [
            _resp("Disbursement vs Target", 55, "Monthly check-off loan disbursement.", ["Total value of check-off loans disbursed per month against the monthly target."]),
            _resp("New Check-off Customers", 15, "Acquire new check-off customers.", ["New customers added per month."]),
            _resp("Loan Quality", 15, "Keep the check-off book healthy.", ["30-day PAR against target."]),
            _resp("Employer and Remittance Compliance", 10, "Maintain employer relationships and deduction remittance.", ["Remittances received on time; employer issues resolved."]),
            _resp(_SELF, 5, "Self (character) and people skills.", ["Integrity, drive, team player"]),
        ],
    ),
    "kpis": [
        _kpi("disbursement", "Disbursement vs target", "kes", "higher", 55, "kpi_entry", "monthly", "Disbursement vs Target"),
        _kpi("new_customers", "New check-off customers", "count", "higher", 15, "kpi_entry", "monthly", "New Check-off Customers"),
        _kpi("par30", "Loan quality - 30-day PAR", "percent", "lower", 15, "kpi_entry", "monthly", "Loan Quality"),
        _kpi("employer_compliance", "Employer and remittance compliance", "rating", "higher", 10, "appraisal_rating", "quarterly", "Employer and Remittance Compliance"),
        _kpi("self_character", "Self (character) and people skills", "rating", "higher", 5, "appraisal_rating", "quarterly", _SELF),
    ],
    "chain": ["staff", "manager", "rm_gm", "hr"],
}

_DEBT_RECOVERY = {  # PROPOSAL - Debt Recovery has no target on today's sheet, so it is not scored until targets are set
    "jd": _jd(
        "Debt Recovery Officer", "", "Commercial",
        "To recover overdue amounts and reduce portfolio at risk.",
        [
            _resp("Amount Recovered", 40, "Cash recovered from overdue accounts.", ["Total amount recovered per month against the monthly recovery target."]),
            _resp("Accounts Resolved", 20, "Accounts cured or otherwise resolved.", ["Number of accounts cured or resolved per month."]),
            _resp("PAR Reduction", 25, "Reduce portfolio at risk on the assigned book.", ["Percentage points of PAR reduced compared with the opening position."]),
            _resp("Promise-to-Pay Kept", 10, "Secure commitments that are honoured.", ["Share of promises to pay that were kept."]),
            _resp(_SELF, 5, "Self (character) and people skills.", ["Integrity, drive, team player"]),
        ],
    ),
    "kpis": [
        _kpi("amount_recovered", "Amount recovered", "kes", "higher", 40, "kpi_entry", "monthly", "Amount Recovered"),
        _kpi("accounts_resolved", "Accounts resolved", "count", "higher", 20, "kpi_entry", "monthly", "Accounts Resolved"),
        _kpi("par_reduction", "PAR reduction (percentage points)", "percent", "higher", 25, "kpi_entry", "monthly", "PAR Reduction"),
        _kpi("promise_kept", "Promise-to-pay kept", "percent", "higher", 10, "kpi_entry", "monthly", "Promise-to-Pay Kept"),
        _kpi("self_character", "Self (character) and people skills", "rating", "higher", 5, "appraisal_rating", "quarterly", _SELF),
    ],
    "chain": ["staff", "manager", "rm_gm", "hr"],
}


def _skeleton(title, department):
    return {"jd": _jd(title, "", department, "", [], DEFAULT_COMPETENCIES), "kpis": [], "chain": ["staff", "manager", "rm_gm", "hr"]}


TEMPLATES: dict[str, dict] = {
    "loan_officer": _LOAN_OFFICER,
    "credit_risk_analyst": _CREDIT_RISK_ANALYST,
    "branch_manager": _BRANCH_MANAGER,
    "auto_champion": _AUTO_CHAMPION,
    "branch_admin": _BRANCH_ADMIN,
    "check_off_agent": _CHECK_OFF,
    "debt_recovery": _DEBT_RECOVERY,
    # Skeletons: HR supplies the JD (upload one in the JD builder); nothing can be scored until then.
    "hr_officer": _skeleton("HR Officer", "Human Resources"),
    "marketing_manager": _skeleton("Marketing Manager", "Marketing"),
    "head_office_staff": _skeleton("Head Office Staff", "Head Office"),
    "sales_team_leader": _skeleton("Sales Team Leader", "Commercial"),
    "regional_manager": _skeleton("Regional Manager", "Commercial"),
    "customer_service_manager": _skeleton("Customer Service Manager", "Operations"),
    "general_manager": _skeleton("General Manager - Operations", "Operations"),
    "ceo": _skeleton("Chief Executive Officer", "Executive"),
}
