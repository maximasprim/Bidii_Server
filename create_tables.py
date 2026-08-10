from app.database import engine, Base

# Import every model so SQLAlchemy registers its tables with Base.metadata
from app.models.admin_user import AdminUser
from app.models.career_application import CareerApplication
from app.models.contact import ContactMessage
from app.models.job_opening import JobOpening
from app.models.loan_application import LoanApplication
from app.models.loan_tier import LoanTier
from app.models.news_article import NewsArticle


print("Creating database tables...")

Base.metadata.create_all(bind=engine)

print("Database tables created successfully.")