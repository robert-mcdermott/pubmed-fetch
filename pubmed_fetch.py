#!/usr/bin/env python3
"""
PubMed Search Utility

A flexible utility to search PubMed for articles with specified MeSH terms
published within a specified time range using the NCBI E-utilities API.
Results are saved in JSON format with full abstracts.

Usage:
    python pubmed_endometrial_search.py --mesh-terms "Endometrial Neoplasms" "Ovarian Neoplasms" --days 30
    python pubmed_endometrial_search.py --config config.ini
"""

import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import time
import sys
import json
import argparse
import configparser
import os

class PubMedSearcher:
    def __init__(self):
        self.base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        self.tool = "gynecologic_neoplasms_search_script"
        self.email = "user@example.com"  # Replace with your email
        
    def search_pubmed(self, mesh_terms, days_back=30, max_results=1000):
        """
        Search PubMed for articles with specific MeSH terms from the past N days
        """
        # Calculate date range (past 30 days)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back)
        
        # Format dates for PubMed API (YYYY/MM/DD)
        start_date_str = start_date.strftime("%Y/%m/%d")
        end_date_str = end_date.strftime("%Y/%m/%d")
        
        # Construct search query with multiple MeSH terms (OR logic)
        if isinstance(mesh_terms, list):
            mesh_queries = [f"{term}[MeSH Terms]" for term in mesh_terms]
            search_term = " OR ".join(mesh_queries)
        else:
            search_term = f"{mesh_terms}[MeSH Terms]"
        
        # E-search parameters
        search_params = {
            'db': 'pubmed',
            'term': search_term,
            'datetype': 'pdat',  # Publication date
            'mindate': start_date_str,
            'maxdate': end_date_str,
            'usehistory': 'y',
            'retmax': max_results,
            'tool': self.tool,
            'email': self.email
        }
        
        mesh_display = mesh_terms if isinstance(mesh_terms, list) else [mesh_terms]
        print(f"Searching PubMed for: {', '.join(mesh_display)}")
        print(f"Search query: {search_term}")
        print(f"Date range: {start_date_str} to {end_date_str}")
        print("=" * 50)
        
        try:
            # Perform search
            search_url = f"{self.base_url}esearch.fcgi"
            response = requests.get(search_url, params=search_params)
            response.raise_for_status()
            
            # Parse XML response
            root = ET.fromstring(response.content)
            
            # Extract PMIDs
            pmids = []
            for id_elem in root.findall('.//Id'):
                pmids.append(id_elem.text)
            
            # Get count
            count_elem = root.find('.//Count')
            total_count = int(count_elem.text) if count_elem is not None else 0
            
            print(f"Found {total_count} articles")
            
            if pmids:
                return self.fetch_article_details(pmids)
            else:
                return []
                
        except requests.RequestException as e:
            print(f"Error searching PubMed: {e}")
            return []
        except ET.ParseError as e:
            print(f"Error parsing XML response: {e}")
            return []
    
    def fetch_article_details(self, pmids):
        """
        Fetch detailed information for articles using PMIDs in batches
        """
        if not pmids:
            return []
        
        print(f"Fetching details for {len(pmids)} articles...")
        
        all_articles = []
        batch_size = 200  # Process in smaller batches
        
        for i in range(0, len(pmids), batch_size):
            batch_pmids = pmids[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (len(pmids) + batch_size - 1) // batch_size
            
            print(f"Processing batch {batch_num}/{total_batches} ({len(batch_pmids)} articles)...")
            
            # E-fetch parameters
            fetch_params = {
                'db': 'pubmed',
                'id': ','.join(batch_pmids),
                'retmode': 'xml',
                'tool': self.tool,
                'email': self.email
            }
            
            try:
                # Add delay to be respectful to NCBI servers
                time.sleep(1)
                
                fetch_url = f"{self.base_url}efetch.fcgi"
                response = requests.get(fetch_url, params=fetch_params)
                response.raise_for_status()
                
                # Parse XML response
                root = ET.fromstring(response.content)
                
                batch_articles = []
                for article in root.findall('.//PubmedArticle'):
                    article_info = self.parse_article(article)
                    if article_info:
                        batch_articles.append(article_info)
                
                all_articles.extend(batch_articles)
                print(f"Retrieved {len(batch_articles)} articles from batch {batch_num}")
                
            except requests.RequestException as e:
                print(f"Error fetching batch {batch_num}: {e}")
                continue
            except ET.ParseError as e:
                print(f"Error parsing batch {batch_num}: {e}")
                continue
        
        print(f"Total articles retrieved: {len(all_articles)}")
        return all_articles
    
    def parse_article(self, article_xml):
        """
        Parse individual article XML to extract relevant information
        """
        try:
            # Extract PMID
            pmid_elem = article_xml.find('.//PMID')
            pmid = pmid_elem.text if pmid_elem is not None else "N/A"
            
            # Extract title
            title_elem = article_xml.find('.//ArticleTitle')
            title = title_elem.text if title_elem is not None else "N/A"
            
            # Extract authors
            authors = []
            for author in article_xml.findall('.//Author'):
                lastname = author.find('LastName')
                forename = author.find('ForeName')
                if lastname is not None and forename is not None:
                    authors.append(f"{lastname.text}, {forename.text}")
                elif lastname is not None:
                    authors.append(lastname.text)
            
            author_str = "; ".join(authors[:5])  # Limit to first 5 authors
            if len(authors) > 5:
                author_str += " et al."
            
            # Extract journal
            journal_elem = article_xml.find('.//Journal/Title')
            journal = journal_elem.text if journal_elem is not None else "N/A"
            
            # Extract publication date
            pub_date = article_xml.find('.//PubDate')
            date_str = "N/A"
            if pub_date is not None:
                year = pub_date.find('Year')
                month = pub_date.find('Month')
                day = pub_date.find('Day')
                
                if year is not None:
                    date_str = year.text
                    if month is not None:
                        date_str += f"-{month.text}"
                        if day is not None:
                            date_str += f"-{day.text}"
            
            # Extract full abstract
            abstract_elements = article_xml.findall('.//Abstract/AbstractText')
            abstract = ""
            if abstract_elements:
                abstract_parts = []
                for elem in abstract_elements:
                    # Handle structured abstracts with labels
                    label = elem.get('Label', '')
                    text = elem.text or ''
                    if label:
                        abstract_parts.append(f"{label}: {text}")
                    else:
                        abstract_parts.append(text)
                abstract = " ".join(abstract_parts).strip()
            
            # Extract MeSH terms
            mesh_terms = []
            for mesh_heading in article_xml.findall('.//MeshHeading'):
                descriptor = mesh_heading.find('DescriptorName')
                if descriptor is not None:
                    mesh_terms.append(descriptor.text)
            
            # Extract DOI if available
            doi = ""
            for article_id in article_xml.findall('.//ArticleId'):
                if article_id.get('IdType') == 'doi':
                    doi = article_id.text
                    break
            
            # Extract PMC ID if available
            pmc_id = ""
            for article_id in article_xml.findall('.//ArticleId'):
                if article_id.get('IdType') == 'pmc':
                    pmc_id = article_id.text
                    break
            
            return {
                'pmid': pmid,
                'title': title,
                'authors': authors,  # Return full list instead of truncated string
                'authors_display': author_str,  # Keep display version
                'journal': journal,
                'date': date_str,
                'abstract': abstract,
                'mesh_terms': mesh_terms,
                'doi': doi,
                'pmc_id': pmc_id,
                'url': f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                'fulltext': None
            }
            
        except Exception as e:
            print(f"Error parsing article: {e}")
            return None
    
    def add_fulltext_info(self, articles):
        """
        Add full-text availability information for all articles
        """
        if not articles:
            return articles
        
        print(f"Checking full-text availability for {len(articles)} articles...")
        
        # Initialize fulltext info for all articles
        for article in articles:
            article['fulltext'] = None
        
        # Handle articles with PMC IDs
        articles_with_pmc = [article for article in articles if article.get('pmc_id')]
        
        if articles_with_pmc:
            # Add PMC links for articles with PMC IDs
            for article in articles_with_pmc:
                pmc_id = article['pmc_id']
                
                # Ensure PMC ID has proper format
                if not pmc_id.startswith('PMC'):
                    pmc_id = f"PMC{pmc_id}"
                
                article['fulltext'] = {
                    'url': f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/",
                    'source': 'PMC',
                    'access_type': 'unknown'  # Will be updated if we can determine
                }
            
            # Check PMC Open Access status in batches
            self._check_pmc_open_access_status(articles_with_pmc)
        
        # Check for other full-text sources (publisher websites, etc.)
        self._check_other_fulltext_sources(articles)
        
        fulltext_count = sum(1 for article in articles if article.get('fulltext'))
        open_access_count = sum(1 for article in articles if article.get('fulltext') and article['fulltext'].get('access_type') == 'open')
        
        print(f"Full-text links found for {fulltext_count} articles ({open_access_count} open access).")
        
        return articles
    
    def _check_pmc_open_access_status(self, articles_with_pmc):
        """
        Check PMC Open Access status for articles with PMC IDs
        """
        batch_size = 50
        for i in range(0, len(articles_with_pmc), batch_size):
            batch = articles_with_pmc[i:i + batch_size]
            pmc_ids = [article['pmc_id'] for article in batch]
            
            try:
                # Format PMC IDs for OA service
                formatted_ids = []
                for pmc_id in pmc_ids:
                    if pmc_id.startswith('PMC'):
                        formatted_ids.append(pmc_id[3:])
                    else:
                        formatted_ids.append(pmc_id)
                
                oa_url = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"
                params = {
                    'id': ','.join(formatted_ids),
                    'tool': self.tool,
                    'email': self.email
                }
                
                time.sleep(0.5)  # Be respectful to the API
                response = requests.get(oa_url, params=params)
                response.raise_for_status()
                
                # Parse XML response
                root = ET.fromstring(response.content)
                
                # Check for Open Access records
                for record in root.findall('.//record'):
                    pmc_id_elem = record.get('id')
                    if not pmc_id_elem:
                        continue
                    
                    # Find corresponding article
                    for article in batch:
                        article_pmc_id = article['pmc_id']
                        if article_pmc_id.startswith('PMC'):
                            article_pmc_id = article_pmc_id[3:]
                        
                        if article_pmc_id == pmc_id_elem:
                            # Extract license and download links
                            license_type = record.get('license', 'unknown')
                            
                            download_links = []
                            for link in record.findall('.//link'):
                                link_format = link.get('format', '')
                                link_url = link.get('href', '')
                                if link_url:
                                    download_links.append({
                                        'format': link_format,
                                        'url': link_url
                                    })
                            
                            # Update fulltext info
                            article['fulltext']['access_type'] = 'open'
                            article['fulltext']['license'] = license_type
                            if download_links:
                                article['fulltext']['download_links'] = download_links
                            break
                
                # Mark remaining PMC articles as closed access
                for article in batch:
                    if article['fulltext']['access_type'] == 'unknown':
                        article['fulltext']['access_type'] = 'closed'
                
            except Exception as e:
                # If we can't check, leave as unknown
                for article in batch:
                    if article['fulltext']['access_type'] == 'unknown':
                        article['fulltext']['access_type'] = 'unknown'
                continue
    
    def _check_other_fulltext_sources(self, articles):
        """
        Check for other full-text sources for articles without PMC IDs
        """
        # For articles without PMC IDs, check if DOI links to open access
        for article in articles:
            if not article.get('fulltext') and article.get('doi'):
                # Create a DOI link - many are open access
                doi_url = f"https://doi.org/{article['doi']}"
                article['fulltext'] = {
                    'url': doi_url,
                    'source': 'Publisher (via DOI)',
                    'access_type': 'unknown'  # Would need additional API calls to determine
                }
    
    def display_results(self, articles, mesh_terms, days_back=30):
        """
        Display search results in a formatted way
        """
        if not articles:
            print("No articles found.")
            return
        
        mesh_display = mesh_terms if isinstance(mesh_terms, list) else [mesh_terms]
        print(f"\nFound {len(articles)} articles with MeSH tags: {', '.join(mesh_display)} (past {days_back} days)")
        print("=" * 80)
        
        for i, article in enumerate(articles, 1):
            print(f"\n{i}. {article['title']}")
            print(f"   Authors: {article['authors_display']}")
            print(f"   Journal: {article['journal']}")
            print(f"   Date: {article['date']}")
            print(f"   PMID: {article['pmid']}")
            print(f"   URL: {article['url']}")
            if article['mesh_terms']:
                print(f"   MeSH Terms: {', '.join(article['mesh_terms'][:5])}{'...' if len(article['mesh_terms']) > 5 else ''}")
            if article['abstract']:
                abstract_preview = article['abstract'][:200] + "..." if len(article['abstract']) > 200 else article['abstract']
                print(f"   Abstract: {abstract_preview}")
            if article.get('pmc_id'):
                print(f"   PMC ID: {article['pmc_id']}")
            if article.get('fulltext'):
                access_indicator = ""
                if article['fulltext']['access_type'] == 'open':
                    access_indicator = " [Open Access]"
                elif article['fulltext']['access_type'] == 'closed':
                    access_indicator = " [Closed Access]"
                
                print(f"   Full-text: {article['fulltext']['url']}{access_indicator}")
                
                if 'download_links' in article['fulltext']:
                    formats = [link['format'] for link in article['fulltext']['download_links']]
                    print(f"   Downloads: {', '.join(formats)} available")
            print("-" * 40)
    
    def save_to_json(self, articles, mesh_terms, filename="pubmed_articles.json", days_back=30):
        """
        Save results to a JSON file
        """
        try:
            mesh_display = mesh_terms if isinstance(mesh_terms, list) else [mesh_terms]
            
            data = {
                "search_info": {
                    "mesh_terms": mesh_display,
                    "generated_on": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "date_range": {
                        "start_date": (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d'),
                        "end_date": datetime.now().strftime('%Y-%m-%d')
                    },
                    "total_articles": len(articles)
                },
                "articles": articles
            }
            
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            print(f"\nResults saved to: {filename}")
            
        except Exception as e:
            print(f"Error saving to file: {e}")

def load_config(config_file):
    """
    Load configuration from INI file
    """
    config = configparser.ConfigParser()
    config.read(config_file)
    
    # Default section
    search_config = {
        'mesh_terms': [],
        'days': 30,
        'max_results': 1000,
        'output_file': 'pubmed_articles.json',
        'email': 'user@example.com'
    }
    
    if 'search' in config:
        section = config['search']
        
        # Parse mesh terms (comma-separated)
        if 'mesh_terms' in section:
            mesh_terms_str = section['mesh_terms']
            search_config['mesh_terms'] = [term.strip() for term in mesh_terms_str.split(',')]
        
        # Parse other settings
        if 'days' in section:
            search_config['days'] = section.getint('days')
        
        if 'max_results' in section:
            search_config['max_results'] = section.getint('max_results')
        
        if 'output_file' in section:
            search_config['output_file'] = section['output_file']
        
        if 'email' in section:
            search_config['email'] = section['email']
    
    return search_config

def parse_arguments():
    """
    Parse command-line arguments
    """
    parser = argparse.ArgumentParser(
        description='Search PubMed for articles with specified MeSH terms',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --mesh-terms "Endometrial Neoplasms" "Ovarian Neoplasms" --days 30
  %(prog)s --config config.ini
  %(prog)s --mesh-terms "Breast Neoplasms" --days 7 --output results.json
  %(prog)s --mesh-terms "COVID-19" --days 30 --max-results 10
        """
    )
    
    parser.add_argument(
        '--mesh-terms', 
        nargs='+', 
        help='MeSH terms to search for (space-separated)'
    )
    
    parser.add_argument(
        '--days', 
        type=int, 
        default=30,
        help='Number of past days to include in search (default: 30)'
    )
    
    parser.add_argument(
        '--max-results', 
        type=int, 
        default=1000,
        help='Maximum number of results to fetch (default: 1000)'
    )
    
    parser.add_argument(
        '--output', 
        help='Output JSON filename (default: pubmed_articles.json)'
    )
    
    parser.add_argument(
        '--email', 
        help='Your email address for NCBI API (recommended)'
    )
    
    parser.add_argument(
        '--config', 
        help='Configuration file (INI format)'
    )
    
    parser.add_argument(
        '--create-config', 
        help='Create a sample configuration file'
    )
    
    
    return parser.parse_args()

def create_sample_config(filename):
    """
    Create a sample configuration file
    """
    config_content = """[search]
# MeSH terms to search for (comma-separated)
mesh_terms = Endometrial Neoplasms, Ovarian Neoplasms

# Number of past days to include in search
days = 30

# Maximum number of results to fetch
max_results = 1000

# Output JSON filename
output_file = pubmed_articles.json

# Your email address (recommended for NCBI API)
email = user@example.com
"""
    
    try:
        with open(filename, 'w') as f:
            f.write(config_content)
        print(f"Sample configuration file created: {filename}")
        print("Edit the file with your desired search parameters.")
        return True
    except Exception as e:
        print(f"Error creating config file: {e}")
        return False

def main():
    """
    Main function to run the PubMed search
    """
    # Parse command-line arguments
    args = parse_arguments()
    
    # Handle config file creation
    if args.create_config:
        create_sample_config(args.create_config)
        return
    
    # Load configuration
    config = {}
    if args.config:
        if not os.path.exists(args.config):
            print(f"Error: Configuration file '{args.config}' not found.")
            sys.exit(1)
        config = load_config(args.config)
    
    # Override config with command-line arguments
    mesh_terms = args.mesh_terms or config.get('mesh_terms', [])
    days = args.days if args.days != 30 else config.get('days', 30)
    max_results = args.max_results if args.max_results != 1000 else config.get('max_results', 1000)
    output_file = args.output or config.get('output_file', 'pubmed_articles.json')
    email = args.email or config.get('email', 'user@example.com')
    
    # Validate inputs
    if not mesh_terms:
        print("Error: No MeSH terms specified. Use --mesh-terms or provide a config file.")
        print("Use --help for usage information or --create-config to create a sample config file.")
        sys.exit(1)
    
    # Initialize searcher with email
    searcher = PubMedSearcher()
    searcher.email = email
    
    # Search for articles
    articles = searcher.search_pubmed(mesh_terms, days_back=days, max_results=max_results)
    
    # Always check for full-text availability
    if articles:
        articles = searcher.add_fulltext_info(articles)
    
    # Display results
    searcher.display_results(articles, mesh_terms, days_back=days)
    
    # Save to JSON file
    if articles:
        searcher.save_to_json(articles, mesh_terms, filename=output_file, days_back=days)
    
    print(f"\nSearch completed. Found {len(articles)} articles.")

if __name__ == "__main__":
    main()